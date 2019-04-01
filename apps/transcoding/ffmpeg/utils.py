import enum
import json
import logging
import os
from pathlib import Path

from apps.transcoding import common
from apps.transcoding.common import ffmpegException
from apps.transcoding.ffmpeg.environment import ffmpegEnvironment
from golem.core.common import HandleError
from golem.docker.image import DockerImage
from golem.docker.job import DockerJob
from golem.docker.task_thread import DockerTaskThread, DockerBind
from golem.environments.environment import Environment
from golem.environments.environmentsmanager import EnvironmentsManager
from golem.resource.dirmanager import DirManager

FFMPEG_DOCKER_IMAGE = 'golemfactory/ffmpeg'
FFMPEG_DOCKER_TAG = '1.0'
FFMPEG_BASE_SCRIPT = '/golem/scripts/ffmpeg_task.py'
FFMPEG_RESULT_FILE = '/golem/scripts/ffmpeg_task.py'

# Suffix used to distinguish the temporary container that has no audio or data
# streams from a complete video
VIDEO_ONLY_CONTAINER_SUFFIX = '[video-only]'

logger = logging.getLogger(__name__)


class Commands(enum.Enum):
    EXTRACT = ('extract', '')
    SPLIT = ('split', 'split-results.json')
    TRANSCODE = ('transcode', '')
    MERGE = ('merge', '')
    REPLACE = ('replace', '')


def adjust_path(path: str,
                dirname: str = None,
                stem: str = None,
                extension: str = None,
                stem_prefix: str = '',
                stem_suffix: str = ''):
    """
    Splits specified path into components and reassembles it back,
    replacing some of those components with user-provided values and adding
    perfixes and suffixes.

    Path components
    ---------------

    # /golem/split/resources/video[num=10].reencoded.mp4
    # =======================^^^^^^^^^^^^^^^^^^^^^^^####
    #         dirname                  stem         extension
    """

    assert extension in [None, ''] or extension.startswith('.'), \
        "Just like in splitext(), the dot must be included in the extension"

    (original_dirname, original_basename) = os.path.split(path)
    (original_stem, original_extension) = os.path.splitext(original_basename)

    new_dirname = original_dirname if dirname is None else dirname
    new_stem = original_stem if stem is None else stem
    new_extension = original_extension if extension is None else extension

    return f"{new_dirname}{stem_prefix}{new_stem}{stem_suffix}{new_extension}"


class StreamOperator:
    def extract_video_streams(self,
                              input_file_on_host: str,
                              dir_manager: DirManager,
                              task_id: str):

        assert os.path.isfile(input_file_on_host), \
            "Caller is responsible for ensuring that input file exists."

        host_dirs = {
            'tmp': dir_manager.get_task_temporary_dir(task_id, create=False),
            'output': dir_manager.get_task_output_dir(task_id),
        }

        input_file_basename = os.path.basename(input_file_on_host)
        input_file_in_container = os.path.join(
            # FIXME: This is a path on the host but docker will create it in
            # the container. It's unlikely that there's anything there but
            # it's not guaranteed.
            host_dirs['tmp'],
            input_file_basename)

        output_file_basename = adjust_path(
            input_file_basename,
            stem_suffix=VIDEO_ONLY_CONTAINER_SUFFIX)
        output_file_on_host = os.path.join(
            host_dirs['output'],
            output_file_basename)
        output_file_in_container = os.path.join(
            DockerJob.OUTPUT_DIR,
            output_file_basename)

        # FIXME: The environment is stored globally. Changing it will affect
        # containers started by other functions that do not do it themselves.
        env = ffmpegEnvironment(binds=[DockerBind(
            Path(input_file_on_host),
            input_file_in_container,
            'ro')])

        extra_data = {
            'script_filepath': FFMPEG_BASE_SCRIPT,
            'command': Commands.EXTRACT.value[0],
            'input_file': input_file_in_container,
            'output_file': output_file_in_container,
            'selected_streams': ['v'],
        }

        logger.debug(
            f'Running video stream extraction [params = {extra_data}]')
        self._do_job_in_container(
            self._get_dir_mapping(dir_manager, task_id),
            extra_data,
            env)

        return output_file_on_host

    @HandleError(ValueError, common.not_valid_json)
    def split_video(self, input_stream: str, parts: int,
                    dir_manager: DirManager, task_id: str):
        name = os.path.basename(input_stream)
        tmp_task_dir = dir_manager.get_task_temporary_dir(task_id)
        stream_container_path = os.path.join(tmp_task_dir, name)
        task_output_dir = dir_manager.get_task_output_dir(task_id)
        env = ffmpegEnvironment(binds=[
            DockerBind(Path(input_stream), stream_container_path, 'ro')])
        extra_data = {
            'script_filepath': FFMPEG_BASE_SCRIPT,
            'command': Commands.SPLIT.value[0],
            'path_to_stream': stream_container_path,
            'parts': parts
        }
        logger.debug('Running video splitting [params = {}]'.format(extra_data))

        result = self._do_job_in_container(
            self._get_dir_mapping(dir_manager, task_id),
            extra_data, env)
        split_result_file = os.path.join(task_output_dir,
                                         Commands.SPLIT.value[1])
        output_files = result.get('data', [])
        if split_result_file not in output_files:
            raise ffmpegException('Result file {} does not exist'.
                                  format(split_result_file))
        logger.debug('Split result file is = {} [parts = {}]'.
                     format(split_result_file, parts))
        with open(split_result_file) as f:
            params = json.load(f)  # FIXME: check status of splitting
            if params.get('status', 'Success') is not 'Success':
                raise ffmpegException('Splitting video failed')
            streams_list = list(map(lambda x: (x.get('video_segment'),
                                               x.get('playlist')),
                                    params.get('segments', [])))
            logger.info('Stream {} was successfully split to {}'
                        .format(input_stream, streams_list))
            return streams_list

    def _prepare_merge_job(self, task_dir, chunks):
        try:
            resources_dir = task_dir
            output_dir = os.path.join(resources_dir, 'merge', 'output')
            os.makedirs(output_dir)
            work_dir = os.path.join(resources_dir, 'merge', 'work')
            os.makedirs(work_dir)
        except OSError:
            raise ffmpegException("Failed to prepare video \
                merge directory structure")
        files = self._collect_files(resources_dir, chunks)
        return resources_dir, output_dir, work_dir, list(
            map(lambda chunk: chunk.replace(resources_dir,
                                            DockerJob.RESOURCES_DIR),
                files))

    @staticmethod
    def _collect_files(dir, files):
        # each chunk must be in the same directory
        results = list()
        for file in files:
            if not os.path.isfile(file):
                raise ffmpegException("Missing result file: {}".format(file))
            elif os.path.dirname(file) != dir:
                raise ffmpegException("Result file: {} should be in the \
                proper directory: {}".format(file, dir))

            results.append(file)

        return results

    def merge_video(self, filename, task_dir, chunks):
        resources_dir, output_dir, work_dir, chunks = \
            self._prepare_merge_job(task_dir, chunks)

        extra_data = {
            'script_filepath': FFMPEG_BASE_SCRIPT,
            'command': Commands.MERGE.value[0],
            'output_stream': os.path.join(DockerJob.OUTPUT_DIR, filename),
            'chunks': chunks
        }

        logger.info('Merging video')
        logger.debug('Merge params: {}'.format(extra_data))

        dir_mapping = DockerTaskThread.specify_dir_mapping(output=output_dir,
                                                           temporary=work_dir,
                                                           resources=task_dir,
                                                           logs=output_dir,
                                                           work=work_dir)

        self._do_job_in_container(dir_mapping, extra_data)

        logger.info("Video merged successfully!")
        return os.path.join(output_dir, filename)

    def replace_video_streams(self,
                              input_file_on_host,
                              merged_file_basename,
                              output_file_basename,
                              task_dir):

        assert os.path.isdir(task_dir), \
            "Caller is responsible for ensuring that task dir exists."
        assert os.path.isfile(input_file_on_host), \
            "Caller is responsible for ensuring that input file exists."

        host_dirs = {
            'resources': task_dir,
            'temporary': os.path.join(task_dir, 'merge', 'work'),
            'work': os.path.join(task_dir, 'merge', 'work'),
            'output': os.path.join(task_dir, 'merge', 'output'),
            'logs': os.path.join(task_dir, 'merge', 'output'),
        }
        container_files = {
            # FIXME: /golem/tmp should not be hard-coded.
            'in': os.path.join(
                '/golem/tmp',
                os.path.basename(input_file_on_host)),
            'merged': os.path.join(DockerJob.OUTPUT_DIR, merged_file_basename),
            'out': os.path.join(DockerJob.OUTPUT_DIR, output_file_basename),
        }
        extra_data = {
            'script_filepath': FFMPEG_BASE_SCRIPT,
            'command': Commands.REPLACE.value[0],
            'input_file': container_files['in'],
            'replacement_source': container_files['merged'],
            'output_file': container_files['out'],
            'stream_type': 'v',
        }

        logger.info('Replacing original video streams with merged ones')
        logger.debug(f'Replace params: {extra_data}')

        # FIXME: The environment is stored globally. Changing it will affect
        # containers started by other functions that do not do it themselves.
        env = ffmpegEnvironment(binds=[DockerBind(
            Path(input_file_on_host),
            container_files['in'],
            'ro')])

        self._do_job_in_container(
            DockerTaskThread.specify_dir_mapping(**host_dirs),
            extra_data,
            env)

        logger.info("Video streams replaced successfully!")

        return os.path.join(host_dirs['output'], output_file_basename)

    @staticmethod
    def _do_job_in_container(dir_mapping, extra_data: dict,
                             env: Environment = None,
                             timeout: int = 120):

        if env:
            EnvironmentsManager().add_environment(env)

        dtt = DockerTaskThread(docker_images=[DockerImage(
            repository=FFMPEG_DOCKER_IMAGE, tag=FFMPEG_DOCKER_TAG)],
            extra_data=extra_data,
            dir_mapping=dir_mapping,
            timeout=timeout)

        dtt.run()
        if dtt.error:
            raise ffmpegException(dtt.error_msg)
        return dtt.result[0] if isinstance(dtt.result, tuple) else dtt.result

    @staticmethod
    def _get_dir_mapping(dir_manager: DirManager, task_id: str):
        tmp_task_dir = dir_manager.get_task_temporary_dir(task_id)
        resources_task_dir = dir_manager.get_task_resource_dir(task_id)
        task_output_dir = dir_manager.get_task_output_dir(task_id)

        return DockerTaskThread. \
            specify_dir_mapping(output=task_output_dir,
                                temporary=tmp_task_dir,
                                resources=resources_task_dir,
                                logs=tmp_task_dir,
                                work=tmp_task_dir)

    @staticmethod
    def _specify_dir_mapping(output, temporary, resources, logs, work):
        return DockerTaskThread.specify_dir_mapping(output=output,
                                                    temporary=temporary,
                                                    resources=resources,
                                                    logs=logs, work=work)
