from ...test_config_base import DebugTestConfig, make_node_config_from_env


class TestConfig(DebugTestConfig):
    def __init__(self):
        super().__init__()
        provider_config = make_node_config_from_env('PROVIDER', 1)
        provider_config.debug = True
        provider_config.script = 'provider/no_wtct_after_ttc'
        provider_config_2 = make_node_config_from_env('PROVIDER', 1)
        provider_config_2.debug = True
        self.provider = [
            provider_config,
            provider_config_2,
        ]
        self.task_settings = '2_short'
