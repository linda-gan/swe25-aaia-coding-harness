"""Tests for per-task acceptance folders and the configured regression checks.

Each task keeps its acceptance tests in its own folder under acceptance/,
and the sandbox mounts only that folder. This keeps tasks independent: the
Stage 1 task must not run (and fail on) another task's acceptance tests.
"""
import pytest

from harness.config import ACCEPTANCE_ROOT, PROJECT_ROOT, Task, load_config, load_task
from harness.runner import check_task, make_sandbox
from harness.sandbox import DockerSandbox

STAGE1_TASK = PROJECT_ROOT / "tasks" / "invalid_quantity.toml"


def write_task(tmp_path, extra: str = "") -> str:
    path = tmp_path / "task.toml"
    path.write_text(
        'description = "Do something."\n'
        'editable_files = ["src/a.py"]\n'
        'agent_checks = ["unit_tests"]\n'
        'final_checks = ["unit_tests"]\n' + extra
    )
    return path


@pytest.fixture
def acceptance_root(tmp_path):
    root = tmp_path / "acceptance"
    (root / "my_task").mkdir(parents=True)
    (root / "my_task" / "test_x.py").write_text("def test_x():\n    pass\n")
    return root


# ---- the real task and folder ---------------------------------------------

def test_stage1_task_uses_its_own_acceptance_folder():
    task = load_task(STAGE1_TASK)
    assert task.acceptance_dir == (ACCEPTANCE_ROOT / "invalid_quantity").resolve()
    assert (task.acceptance_dir / "test_acceptance_invalid_quantity.py").is_file()


def test_no_acceptance_tests_lie_directly_in_the_shared_folder():
    # A test file here would be collected for no task, or for all of them.
    assert not list(ACCEPTANCE_ROOT.glob("*.py"))


# ---- loading and refusing acceptance folders ------------------------------

def test_task_without_acceptance_folder_has_none(tmp_path, acceptance_root):
    task = load_task(write_task(tmp_path), acceptance_root=acceptance_root)
    assert task.acceptance_dir is None


def test_task_names_a_folder_under_acceptance(tmp_path, acceptance_root):
    path = write_task(tmp_path, 'acceptance_tests = "my_task"\n')
    task = load_task(path, acceptance_root=acceptance_root)
    assert task.acceptance_dir == (acceptance_root / "my_task").resolve()


@pytest.mark.parametrize("name", ["../secrets", "/etc", "my_task/sub", ".", "..", "", "my task"])
def test_folder_names_that_could_point_elsewhere_are_refused(tmp_path, acceptance_root, name):
    path = write_task(tmp_path, f'acceptance_tests = "{name}"\n')
    with pytest.raises(ValueError, match="plain name of a folder"):
        load_task(path, acceptance_root=acceptance_root)


def test_folder_name_must_be_text(tmp_path, acceptance_root):
    path = write_task(tmp_path, "acceptance_tests = 5\n")
    with pytest.raises(ValueError, match="plain name of a folder"):
        load_task(path, acceptance_root=acceptance_root)


def test_missing_folder_is_refused(tmp_path, acceptance_root):
    path = write_task(tmp_path, 'acceptance_tests = "no_such_task"\n')
    with pytest.raises(ValueError, match="does not exist"):
        load_task(path, acceptance_root=acceptance_root)


def test_symlinked_folder_pointing_outside_is_refused(tmp_path, acceptance_root):
    outside = tmp_path / "private"
    outside.mkdir()
    (acceptance_root / "sneaky").symlink_to(outside, target_is_directory=True)
    path = write_task(tmp_path, 'acceptance_tests = "sneaky"\n')
    with pytest.raises(ValueError, match="points outside"):
        load_task(path, acceptance_root=acceptance_root)


# ---- early checks and the sandbox mount -----------------------------------

def test_task_using_acceptance_check_without_folder_is_refused_early():
    task = Task("x", ["a.py"], ["unit_tests"], ["acceptance", "unit_tests"])
    with pytest.raises(ValueError, match="names no acceptance folder"):
        check_task(task, load_config())


def test_task_without_acceptance_check_needs_no_folder():
    task = Task("x", ["a.py"], ["unit_tests"], ["unit_tests", "integration_sqlite"])
    check_task(task, load_config())  # no error


def test_sandbox_gets_only_the_tasks_acceptance_folder(tmp_path):
    task = load_task(STAGE1_TASK)
    received = {}

    def factory(repo, sandbox_config, checks, **kwargs):
        received.update(kwargs)

    class Copy:  # the part of RunCopy that make_sandbox uses
        repo = tmp_path

    make_sandbox(Copy(), load_config(), task, sandbox_factory=factory)
    assert received["acceptance_dir"] == task.acceptance_dir
    assert received["acceptance_dir"] != ACCEPTANCE_ROOT


def test_docker_mounts_the_tasks_folder_read_only(tmp_path):
    task = load_task(STAGE1_TASK)
    config = load_config()
    sandbox = DockerSandbox(tmp_path, config.sandbox, config.checks,
                            acceptance_dir=task.acceptance_dir)
    command = " ".join(sandbox.build_create_command("c", ["true"]))
    assert f"src={task.acceptance_dir},dst=/acceptance,readonly" in command


# ---- the SQLite integration check -----------------------------------------

def test_sqlite_integration_check_runs_only_tests_that_need_no_services():
    argv = load_config().checks["integration_sqlite"]
    files = [part for part in argv if part.startswith("tests/")]
    assert argv[0] == "pytest"
    assert files == [
        "tests/integration/test_repository.py",
        "tests/integration/test_uow.py",
        "tests/integration/test_views.py",
    ]
    # The Postgres test is left out by name; e2e and e-mail tests are not listed.
    assert "not test_concurrent_updates_to_version_are_not_allowed" in argv
    assert "no:cacheprovider" in argv
