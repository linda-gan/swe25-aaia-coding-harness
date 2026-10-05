from harness.config import load_target_config


def test_loads_target_config():
    cfg = load_target_config()
    assert cfg.url == "https://github.com/cosmicpython/code.git"
    assert len(cfg.commit) == 40
    assert cfg.regression_tests == "tests/unit"


def test_loads_sandbox_settings_and_checks():
    from harness.config import load_config

    cfg = load_config()
    assert cfg.sandbox.image == "cosmic-sandbox"
    assert cfg.sandbox.timeout_seconds > 0
    assert set(cfg.checks) == {"unit_tests", "acceptance"}
    assert cfg.checks["unit_tests"][0] == "pytest"
