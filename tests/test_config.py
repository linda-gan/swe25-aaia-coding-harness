from harness.config import load_target_config


def test_loads_target_config():
    cfg = load_target_config()
    assert cfg.url == "https://github.com/cosmicpython/code.git"
    assert len(cfg.commit) == 40
    assert cfg.regression_tests == "tests/unit"
