from harness.config import PROJECT_ROOT, WORKSPACE_DIR, load_config, load_task


def test_loads_the_allowed_project():
    project = load_config().project("cosmicpython")
    assert project.url == "https://github.com/cosmicpython/code.git"
    assert len(project.commit) == 40
    assert project.pristine == (WORKSPACE_DIR / "target-pristine").resolve()


def test_loads_sandbox_settings_and_checks():
    project = load_config().project("cosmicpython")
    assert project.sandbox.image == "cosmic-sandbox"
    assert project.sandbox.timeout_seconds > 0
    assert set(project.checks) == {"unit_tests", "acceptance", "integration_sqlite"}
    assert project.checks["unit_tests"][0] == "pytest"


def test_loads_limits():
    limits = load_config().limits
    assert limits.max_actions > 0
    assert limits.max_tool_output_chars > 0


def test_loads_task_and_its_checks_exist():
    task = load_task(PROJECT_ROOT / "tasks" / "invalid_quantity.toml")
    assert task.project == "cosmicpython"
    assert "InvalidQuantity" in task.description
    assert "src/allocation/service_layer/handlers.py" in task.editable_files
    assert set(task.agent_checks) <= set(load_config().project("cosmicpython").checks)
    assert task.final_checks == ["acceptance", "unit_tests"]


def test_loads_model_settings():
    model = load_config().model
    assert model.host.startswith("http://")
    assert model.name
    assert model.num_ctx >= 16384
