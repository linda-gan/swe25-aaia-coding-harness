"""Tests for run copies, using a small real Git repository as the pristine copy."""
import subprocess

import pytest

from harness.workspace import WorkspaceError, changed_files, create_run_copy, diff, git


def commit_all(repo, message="start"):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", message],
        check=True,
    )


@pytest.fixture
def pristine(tmp_path):
    repo = tmp_path / "pristine"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "model.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text(".venv/\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    commit_all(repo)
    (repo / ".venv").mkdir()  # an ignored local environment, as in the real pristine copy
    (repo / ".venv" / "big.bin").write_text("not needed in runs")
    return repo


@pytest.fixture
def commit(pristine):
    return git(pristine, "rev-parse", "HEAD").strip()


def test_run_copy_contains_code_and_git_but_not_local_environment(pristine, commit, tmp_path):
    run = create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")

    assert run.repo == run.run_dir / "repo"
    assert (run.repo / "src" / "model.py").read_text() == "x = 1\n"
    assert (run.repo / ".git").is_dir()
    assert not (run.repo / ".venv").exists()
    assert diff(run.repo) == ""  # starts clean at the pinned commit


def test_each_run_gets_its_own_folder(pristine, commit, tmp_path):
    first = create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")
    second = create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")
    assert first.run_dir != second.run_dir


def test_refuses_missing_pristine_copy(tmp_path):
    with pytest.raises(WorkspaceError, match="fetch_target.sh"):
        create_run_copy("abc", pristine=tmp_path / "missing", runs_dir=tmp_path / "runs")


def test_refuses_pristine_copy_at_wrong_commit(pristine, tmp_path):
    with pytest.raises(WorkspaceError, match="expects"):
        create_run_copy("0" * 40, pristine=pristine, runs_dir=tmp_path / "runs")


def test_refuses_pristine_copy_with_local_changes(pristine, commit, tmp_path):
    (pristine / "src" / "model.py").write_text("x = 2\n")
    with pytest.raises(WorkspaceError, match="local changes"):
        create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")


def test_diff_and_changed_files_show_edits_and_new_files(pristine, commit, tmp_path):
    run = create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")
    (run.repo / "src" / "model.py").write_text("x = 2\n")
    (run.repo / "src" / "new.py").write_text("y = 1\n")

    assert "-x = 1" in diff(run.repo)
    assert "+x = 2" in diff(run.repo)
    assert changed_files(run.repo) == [("M", "src/model.py"), ("??", "src/new.py")]
