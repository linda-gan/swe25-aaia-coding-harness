"""Tests for allowed projects: the config allowlist, selecting a project for a
run, fetching a pristine clone, and the projects/fetch commands.

Fetch tests clone a small local Git repository, so no network is needed.
"""
import subprocess

import pytest

from harness.cli import Style, format_projects, main
from harness.config import (
    PROJECT_ROOT, ProjectConfig, SandboxConfig, Task, load_config, load_task,
)
from harness.projects import TaskEntry, list_tasks, pristine_status, select_project
from harness.runner import check_task
from harness.workspace import WorkspaceError, create_project_copy, fetch_project, git

COMMIT = "14c84797ffa77255d53cf1a02fe6aafda2b68aeb"
SHARED = '''
[sandbox]
timeout_seconds = 60
memory = "512m"
cpus = "1"
max_output_chars = 2000

[limits]
max_actions = 5
max_denied_actions = 2
max_retries = 1
max_tool_output_chars = 1000

[model]
host = "http://localhost:11434"
name = "m"
think = false
num_ctx = 16384
timeout_seconds = 10
'''


def project_toml(name="alpha", pristine="workspace/alpha", image="alpha-image",
                 commit=COMMIT, extra=""):
    return (f'[projects.{name}]\nurl = "https://example.com/{name}.git"\n'
            f'commit = "{commit}"\npristine = "{pristine}"\nimage = "{image}"\n{extra}'
            f'[projects.{name}.checks]\ntests = ["pytest", "tests"]\n')


def write_config(tmp_path, projects: str, shared: str = SHARED):
    path = tmp_path / "config.toml"
    path.write_text(projects + shared)
    return path


def load(tmp_path, projects, shared=SHARED):
    return load_config(write_config(tmp_path, projects, shared), workspace_dir=tmp_path / "workspace")


# ---- the allowlist in the config ------------------------------------------

def test_real_config_allows_cosmicpython():
    assert list(load_config().projects) == ["cosmicpython"]


def test_each_project_has_its_own_image_and_checks(tmp_path):
    config = load(tmp_path, project_toml("alpha") + project_toml("beta", "workspace/beta", "beta-image"))
    assert config.project("alpha").sandbox.image == "alpha-image"
    assert config.project("beta").sandbox.image == "beta-image"
    assert config.project("beta").sandbox.memory == "512m"  # shared resources
    assert config.project("alpha").pristine == (tmp_path / "workspace" / "alpha").resolve()


def test_unknown_project_is_refused_with_the_allowed_names(tmp_path):
    config = load(tmp_path, project_toml("alpha"))
    with pytest.raises(ValueError, match=r"'gamma' is not allowed\. Allowed projects: alpha"):
        config.project("gamma")


@pytest.mark.parametrize("pristine", [
    "../elsewhere", "/etc", "workspace", "workspace/runs/x", "workspace/../outside",
])
def test_pristine_clone_must_stay_inside_the_workspace(tmp_path, pristine):
    with pytest.raises(ValueError, match="pristine"):
        load(tmp_path, project_toml(pristine=pristine))


def test_two_projects_cannot_share_a_pristine_clone(tmp_path):
    with pytest.raises(ValueError, match="same pristine"):
        load(tmp_path, project_toml("alpha") + project_toml("beta", pristine="workspace/alpha"))


@pytest.mark.parametrize("projects, message", [
    (project_toml(commit="abc123"), "40-character"),
    (project_toml(image=""), "image"),
    (project_toml(extra='branch = "main"\n'), "unknown setting"),
    (project_toml().replace("[projects.alpha", '[projects."bad name"'), "project name"),
])
def test_invalid_project_settings_are_refused(tmp_path, projects, message):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, projects)


def test_config_without_projects_is_refused(tmp_path):
    with pytest.raises(ValueError, match="projects"):
        load(tmp_path, "")


def test_image_belongs_to_the_project_not_the_shared_sandbox(tmp_path):
    shared = SHARED.replace('[sandbox]\n', '[sandbox]\nimage = "x"\n')
    with pytest.raises(ValueError, match="each project sets its own"):
        load(tmp_path, project_toml(), shared)


# ---- task files name their project ----------------------------------------

def test_task_files_name_their_project():
    for entry in list_tasks():
        assert entry.task is not None, entry.error
        assert entry.task.project == "cosmicpython"


def task_file(tmp_path, project_line):
    path = tmp_path / "task.toml"
    path.write_text(project_line + 'description = "x"\neditable_files = ["a.py"]\n'
                    'agent_checks = []\nfinal_checks = ["tests"]\n')
    return path


def test_task_without_project_is_refused(tmp_path):
    with pytest.raises(ValueError, match="project"):
        load_task(task_file(tmp_path, ""))


def test_task_project_must_be_a_plain_name(tmp_path):
    with pytest.raises(ValueError, match="plain name of a project"):
        load_task(task_file(tmp_path, 'project = "../alpha"\n'))


# ---- selecting the project for a run --------------------------------------

@pytest.fixture
def two_projects(tmp_path):
    return load(tmp_path, project_toml("alpha") + project_toml("beta", "workspace/beta"))


def make_task(project):
    return Task("x", ["a.py"], [], ["tests"], project=project)


def test_task_decides_the_project_by_default(two_projects):
    assert select_project(two_projects, make_task("beta")).name == "beta"


def test_requested_project_must_match_the_task(two_projects):
    assert select_project(two_projects, make_task("beta"), "beta").name == "beta"
    with pytest.raises(ValueError, match="belongs to project 'beta', not 'alpha'"):
        select_project(two_projects, make_task("beta"), "alpha")


def test_requested_project_must_be_allowed(two_projects):
    with pytest.raises(ValueError, match="not allowed"):
        select_project(two_projects, make_task("beta"), "gamma")


def test_task_for_a_project_that_is_not_allowed_is_refused(two_projects):
    with pytest.raises(ValueError, match="not allowed"):
        select_project(two_projects, make_task("gamma"))


def test_checks_come_from_the_selected_project(two_projects):
    task = Task("x", ["a.py"], [], ["unit_tests"], project="alpha")
    with pytest.raises(ValueError, match=r"\[projects\.alpha\.checks\]"):
        check_task(task, two_projects.project("alpha"))


# ---- fetching a pristine clone (from a local repository) -------------------

@pytest.fixture
def source(tmp_path):
    """A small upstream repository with two commits."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for text in ["v1\n", "v2\n"]:
        (repo / "app.py").write_text(text)
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.com",
                        "commit", "-q", "-m", text.strip()], check=True)
    return repo


def local_project(tmp_path, source, commit=None):
    first = git(source, "rev-list", "--max-parents=0", "HEAD").strip()
    return ProjectConfig(
        name="demo", url=str(source), commit=commit or first,
        pristine=tmp_path / "workspace" / "demo-pristine",
        sandbox=SandboxConfig("img", 60, "512m", "1", 2000), checks={"tests": ["true"]},
    )


def test_fetch_makes_a_clone_at_the_pinned_commit_without_remote(tmp_path, source):
    project = local_project(tmp_path, source)
    assert fetch_project(project) is True
    assert git(project.pristine, "rev-parse", "HEAD").strip() == project.commit
    assert (project.pristine / "app.py").read_text() == "v1\n"  # not the newest commit
    assert git(project.pristine, "remote").strip() == ""  # nothing to push to
    assert pristine_status(project) == "ready"


def test_fetch_leaves_a_good_clone_alone(tmp_path, source):
    project = local_project(tmp_path, source)
    fetch_project(project)
    marker = project.pristine / "untracked-note.txt"
    marker.write_text("still here")  # untracked files do not make a clone unusable
    assert fetch_project(project) is False
    assert marker.exists()


def test_fetch_replaces_a_changed_clone(tmp_path, source):
    project = local_project(tmp_path, source)
    fetch_project(project)
    (project.pristine / "app.py").write_text("edited\n")
    assert "local changes" in pristine_status(project)
    assert fetch_project(project) is True
    assert (project.pristine / "app.py").read_text() == "v1\n"


def test_force_fetches_again(tmp_path, source):
    project = local_project(tmp_path, source)
    fetch_project(project)
    marker = project.pristine / "untracked-note.txt"
    marker.write_text("x")
    assert fetch_project(project, force=True) is True
    assert not marker.exists()


def test_failed_fetch_leaves_nothing_behind(tmp_path, source):
    project = local_project(tmp_path, source, commit="0" * 40)  # not in the repository
    with pytest.raises(WorkspaceError):
        fetch_project(project)
    assert not project.pristine.exists()
    assert list((tmp_path / "workspace").iterdir()) == []  # no temporary folder left


def test_missing_clone_tells_how_to_fetch_it(tmp_path, source):
    project = local_project(tmp_path, source)
    assert "No pristine copy" in pristine_status(project)
    assert "python -m harness fetch demo" in pristine_status(project)
    with pytest.raises(WorkspaceError, match="python -m harness fetch demo"):
        create_project_copy(project, runs_dir=tmp_path / "runs")


def test_run_copy_comes_from_the_projects_clone(tmp_path, source):
    project = local_project(tmp_path, source)
    fetch_project(project)
    run_copy = create_project_copy(project, runs_dir=tmp_path / "runs")
    assert (run_copy.repo / "app.py").read_text() == "v1\n"


# ---- the CLI ---------------------------------------------------------------

def test_projects_lists_projects_tasks_and_problems(two_projects, tmp_path):
    tasks = [
        TaskEntry(tmp_path / "a.toml", make_task("alpha")),
        TaskEntry(tmp_path / "g.toml", make_task("gamma")),
        TaskEntry(tmp_path / "bad.toml", None, "missing setting"),
    ]
    statuses = {"alpha": "ready", "beta": "No pristine copy at x."}
    text = format_projects(Style(False), two_projects, statuses, tasks)
    assert "== alpha" in text and "== beta" in text
    assert "alpha-image" in text and "(ready)" in text and "No pristine copy" in text
    assert "not allowed (cannot run)" in text and "'gamma'" in text
    assert "could not be loaded" in text and "missing setting" in text


def test_projects_command_shows_the_real_projects(capsys):
    assert main(["projects"]) == 0
    out = capsys.readouterr().out
    assert "cosmicpython" in out
    assert "tasks/invalid_quantity.toml" in out and "tasks/batch_expiry.toml" in out


@pytest.mark.parametrize("argv", [
    ["baseline", "tasks/batch_expiry.toml", "--project", "unknown"],
    ["run", "tasks/batch_expiry.toml", "--project", "unknown"],
    ["fetch", "unknown"],
])
def test_project_that_is_not_allowed_stops_before_anything_runs(argv, capsys, monkeypatch):
    monkeypatch.chdir(PROJECT_ROOT)

    def no_copies(*args, **kwargs):
        raise AssertionError("no run copy may be made")

    monkeypatch.setattr("harness.cli.create_project_copy", no_copies)
    assert main(argv) == 2
    assert "not allowed" in capsys.readouterr().err
