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


def test_loads_limits():
    from harness.config import load_config

    limits = load_config().limits
    assert limits.max_actions > 0
    assert limits.max_tool_output_chars > 0


def test_loads_task_and_its_checks_exist():
    from harness.config import PROJECT_ROOT, load_config, load_task

    task = load_task(PROJECT_ROOT / "tasks" / "invalid_quantity.toml")
    assert "InvalidQuantity" in task.description
    assert "src/allocation/service_layer/handlers.py" in task.editable_files
    assert set(task.agent_checks) <= set(load_config().checks)


def test_loads_model_settings():
    from harness.config import load_config

    model = load_config().model
    assert model.host.startswith("http://")
    assert model.name
    assert model.num_ctx >= 16384
