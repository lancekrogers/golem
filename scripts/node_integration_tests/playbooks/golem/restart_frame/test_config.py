from ...test_config_base import DebugTestConfig, make_node_config_from_env


class TestConfig(DebugTestConfig):
    def __init__(self):
        super().__init__()
        requestor_config = make_node_config_from_env('REQUESTOR', 0)
        requestor_config.debug = True
        requestor_config_2 = make_node_config_from_env('REQUESTOR', 0)
        requestor_config_2.debug = True
        requestor_config_2.script = 'requestor/always_accept_provider'
        self.requestor = [
            requestor_config,
            requestor_config_2,
        ]
        self.task_settings = 'default'
