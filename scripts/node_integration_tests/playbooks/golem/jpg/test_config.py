from ...test_config_base import DebugTestConfig


class TestConfig(DebugTestConfig):
    def __init__(self):
        super().__init__()
        self.task_settings = 'jpg'
