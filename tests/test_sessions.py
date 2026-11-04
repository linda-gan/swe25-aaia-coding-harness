"""Tests for sessions: saving, reopening after a restart, listing, showing,
and detecting interrupted runs with an uncertain action.

Handout test "Session storage": reopen saved task details, steps, history,
errors and check results after the application restarts. "Restarts" is
tested for real: a separate Python process reads the session, and a harness
process is killed while a check is running.
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import replace

import pytest

from harness.cli import Style, format_session, format_sessions, main
from harness.config import ACCEPTANCE_ROOT, PROJECT_ROOT, Task, load_config
from harness.controller import FINISHED
from harness.model import ScriptedModelClient, tool_reply
from harness.runner import run_task
from harness.sandbox import CheckResult
from harness.session import (
    FAILED, INTERRUPTED, PASSED, RUNNING, SESSION_FILE, Session, SessionError, SessionStore,
    fingerprint,
)
from harness.workspace import create_run_copy, git

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
TASK = Task(
    description="Reject quantities of zero or less.",
    editable_files=["src/app/model.py"],
    agent_checks=["unit_tests"],
    final_checks=["acceptance", "unit_tests"],
    acceptance_dir=ACCEPTANCE_ROOT / "invalid_quantity",
    project="cosmicpython",
)
FIX = tool_reply("edit_file", path="src/app/model.py",
                 old="return qty <= 10", new="return 0 < qty <= 10")


class FixAwareSandbox:
    def __init__(self, repo, *args, **kwargs):
        self.repo = repo

    def run_check(self, name):
        fixed = "0 < qty" in (self.repo / "src/app/model.py").read_text()
        code, out = (0, "3 passed") if fixed else (1, "FAILED test_x\n1 failed")
        return CheckResult(name, ["pytest"], code, out, False, 0.1, "fake")


def make_pristine(tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "src" / "app").mkdir(parents=True)
    (pristine / "src" / "app" / "model.py").write_text(ORIGINAL)
    subprocess.run(["git", "init", "-q", str(pristine)], check=True)
    subprocess.run(["git", "-C", str(pristine), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(pristine), "-c", "user.name=T", "-c", "user.email=t@e.com",
                    "commit", "-q", "-m", "start"], check=True)
    return pristine, git(pristine, "rev-parse", "HEAD").strip()


@pytest.fixture
def setup(tmp_path):
    pristine, commit = make_pristine(tmp_path)
    runs = tmp_path / "runs"
    project = replace(load_config().project("cosmicpython"), commit=commit)
    return pristine, commit, runs, project


def new_copy(setup):
    pristine, commit, runs, _ = setup
    return create_run_copy(commit, pristine=pristine, runs_dir=runs)


def full_run(setup):
    """A run with a refused request, a failing check, a fix and a passing finish."""
    run_copy = new_copy(setup)
    model = ScriptedModelClient([
        tool_reply("read_file", path="../secret.txt"),          # error
        tool_reply("run_check", name="unit_tests"),             # fails
        FIX,
        tool_reply("finish", summary="Quantities must be positive."),
    ])
    report = run_task(task=TASK, task_path="tasks/test.toml", config=load_config(),
                      project=setup[3], model=model, model_name="scripted",
                      run_copy=run_copy, sandbox_factory=FixAwareSandbox)
    return run_copy, report


# ---- saving and reopening ---------------------------------------------------------

def test_session_is_saved_and_reopened_with_everything(setup):
    run_copy, report = full_run(setup)
    assert report.agent.stop_reason == FINISHED

    session = SessionStore(setup[2]).open(run_copy.run_id)  # a fresh store and object
    data = session.data
    assert session.status == PASSED and data["verification"]["passed"] is True
    # task details and steps (from the task record)
    record = session.task_record()
    assert record.data["request"] == TASK.description
    assert record.data["allowed_changes"] == ["src/app/model.py"]
    assert [s["tool"] for s in record.steps] == ["read_file", "run_check", "edit_file", "finish"]
    assert record.steps[0]["ok"] is False and record.steps[0]["status"] == "error"
    # history
    assert len(data["messages"]) == len(report.agent.messages)
    assert data["messages"][1]["content"] == TASK.description
    # errors
    assert data["errors"][0]["step"] == 1
    assert "outside the repository" in data["errors"][0]["message"]
    # check results: by the model, at finish, and the final verification
    assert [(c["by"], c["status"]) for c in data["checks"]] == [("model", "failed"),
                                                                ("finish", "passed")]
    assert {c["name"]: c["status"] for c in data["verification"]["checks"]} == \
        {"acceptance": "passed", "unit_tests": "passed"}
    # limits used, and the state of the working copy including the harness's edit
    assert data["counters"] == {"actions": 3, "denied": 1, "retries": 0, "fix_rounds": 0,
                                "steps": 4}
    assert set(data["working_copy"]["files"]) == {"src/app/model.py"}
    assert data["context"]["nonce"] and data["pending_action"] is None


def test_a_separate_process_can_show_the_saved_session(setup):
    run_copy, _ = full_run(setup)
    out = subprocess.run(
        [sys.executable, "-m", "harness", "show", run_copy.run_id[-6:], "--history",
         "--runs-dir", str(setup[2])],
        cwd=PROJECT_ROOT, capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert f"Session {run_copy.run_id}" in text and "Status:   passed" in text
    assert "Reject quantities of zero or less." in text          # task details
    assert "edit_file src/app/model.py  [ok]" in text             # steps
    assert "outside the repository" in text                       # errors
    assert "by model  unit_tests" in text and "FAILED" in text    # check results
    assert "RESULT: PASSED" in text
    assert "messages saved" in text and "assistant" in text       # history


def test_listing_shows_status_steps_and_limits(setup):
    full_run(setup)
    sessions = SessionStore(setup[2]).all()
    text = format_sessions(Style(False), sessions)
    assert "passed" in text and "3/30" in text  # 3 of the default 30 actions


def test_run_folders_without_a_session_are_skipped(setup):
    new_copy(setup)  # e.g. a run from before sessions existed
    assert SessionStore(setup[2]).all() == []
    assert "No sessions yet" in format_sessions(Style(False), [])


# ---- finding sessions --------------------------------------------------------------

def test_sessions_are_found_by_id_or_a_unique_part(setup):
    run_copy, _ = full_run(setup)
    store = SessionStore(setup[2])
    rid = run_copy.run_id
    for ref in [rid, rid[:15], rid[-6:]]:
        assert store.open(ref).session_id == rid


@pytest.mark.parametrize("ref, message", [
    ("nomatch", "No session matches"),
    ("../etc", "not a session ID"),
    ("a/b", "not a session ID"),
])
def test_unknown_or_unsafe_references_are_refused(setup, ref, message):
    full_run(setup)
    with pytest.raises(SessionError, match=message):
        SessionStore(setup[2]).open(ref)


def test_ambiguous_reference_is_refused(setup):
    full_run(setup)
    full_run(setup)
    with pytest.raises(SessionError, match="matches several sessions"):
        SessionStore(setup[2]).open("2")  # both IDs start with the year


def test_cli_reports_an_unknown_session_as_a_setup_problem(setup, capsys):
    assert main(["show", "nomatch", "--runs-dir", str(setup[2])]) == 2
    assert "No session matches" in capsys.readouterr().err


# ---- running, pending and interrupted ---------------------------------------------

def test_a_session_in_use_is_running_and_cannot_be_opened_twice(setup):
    run_copy = new_copy(setup)
    session = Session.create(run_copy, "t", setup[3], "m")
    try:
        assert Session.load(run_copy.run_dir).status == RUNNING  # another reader sees it locked
        with pytest.raises(SessionError, match="already in use"):
            Session.create(run_copy, "t", setup[3], "m")
    finally:
        session.close()
    assert Session.load(run_copy.run_dir).status == INTERRUPTED  # lock gone, never finished


def test_pending_action_is_saved_before_the_action_runs(setup):
    run_copy = new_copy(setup)
    seen = {}

    class PeekingSandbox(FixAwareSandbox):
        def run_check(self, name):
            data = json.loads((run_copy.run_dir / SESSION_FILE).read_text())
            seen.setdefault("pending", data["pending_action"])
            return super().run_check(name)

    model = ScriptedModelClient([FIX, tool_reply("run_check", name="unit_tests"),
                                 tool_reply("finish", summary="done")])
    run_task(task=TASK, task_path="t", config=load_config(), project=setup[3], model=model,
             model_name="m", run_copy=run_copy, sandbox_factory=PeekingSandbox)
    pending = seen["pending"]
    assert pending["tool"] == "run_check" and pending["target"] == "unit_tests"
    assert pending["step"] == 2 and pending["action"] == 2
    assert Session.load(run_copy.run_dir).data["pending_action"] is None  # cleared afterwards


def test_an_unhandled_stop_during_a_check_leaves_an_interrupted_session(setup):
    # A KeyboardInterrupt raised directly (not through the run's signal handling,
    # which turns Ctrl+C into a cancel) stands for a stop the harness cannot handle.
    run_copy = new_copy(setup)

    class InterruptedSandbox(FixAwareSandbox):
        def run_check(self, name):
            raise KeyboardInterrupt

    model = ScriptedModelClient([FIX, tool_reply("run_check", name="unit_tests")])
    with pytest.raises(KeyboardInterrupt):
        run_task(task=TASK, task_path="t", config=load_config(), project=setup[3], model=model,
                 model_name="m", run_copy=run_copy, sandbox_factory=InterruptedSandbox)
    session = SessionStore(setup[2]).open(run_copy.run_id)
    assert session.status == INTERRUPTED
    assert session.uncertain_action["tool"] == "run_check"
    assert session.data["counters"]["steps"] == 1  # the edit was completed and saved
    text = format_session(Style(False), session)
    assert "Uncertain action: step 2 (run_check unit_tests)" in text
    assert "not run (the session was interrupted)" in text


CHILD = textwrap.dedent('''
    import sys, time
    from dataclasses import replace
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from harness.config import ACCEPTANCE_ROOT, Task, load_config
    from harness.model import ScriptedModelClient, tool_reply
    from harness.runner import run_task
    from harness.workspace import RunCopy

    run_dir = Path(sys.argv[2])
    run_copy = RunCopy(run_dir.name, run_dir, run_dir / "repo")
    task = Task("Reject quantities.", ["src/app/model.py"], ["unit_tests"], ["unit_tests"],
                project="cosmicpython")
    project = replace(load_config().project("cosmicpython"), commit=sys.argv[3])

    class StuckSandbox:
        def __init__(self, *args, **kwargs):
            pass
        def run_check(self, name):
            time.sleep(120)  # killed long before this ends

    model = ScriptedModelClient([
        tool_reply("edit_file", path="src/app/model.py",
                   old="return qty <= 10", new="return 0 < qty <= 10"),
        tool_reply("run_check", name="unit_tests"),
    ])
    run_task(task=task, task_path="t", config=load_config(), project=project, model=model,
             model_name="m", run_copy=run_copy, sandbox_factory=StuckSandbox)
''')


def test_a_killed_harness_process_leaves_an_interrupted_session(setup, tmp_path):
    run_copy = new_copy(setup)
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    child = subprocess.Popen([sys.executable, str(script), str(PROJECT_ROOT),
                              str(run_copy.run_dir), setup[1]])
    session_file = run_copy.run_dir / SESSION_FILE
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:  # wait until the check is in progress
            if session_file.exists():
                pending = json.loads(session_file.read_text())["pending_action"]
                if pending and pending["tool"] == "run_check":
                    break
            time.sleep(0.1)
        else:
            pytest.fail("the child never started its check")
        assert Session.load(run_copy.run_dir).status == RUNNING  # alive: lock held
        os.kill(child.pid, signal.SIGKILL)  # no chance to clean up
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    session = SessionStore(setup[2]).open(run_copy.run_id)
    assert session.status == INTERRUPTED
    assert session.uncertain_action["tool"] == "run_check"
    # The harness's own edit, completed before the kill, is in the saved fingerprint
    # and matches the working copy.
    assert session.data["working_copy"] == fingerprint(run_copy.repo)
    assert "src/app/model.py" in session.data["working_copy"]["files"]


# ---- the fingerprint --------------------------------------------------------------

def test_fingerprint_follows_edits_and_deletions(setup):
    run_copy = new_copy(setup)
    clean = fingerprint(run_copy.repo)
    assert clean["files"] == {} and clean["head"] == setup[1]
    (run_copy.repo / "src/app/model.py").write_text("changed\n")
    first = fingerprint(run_copy.repo)["files"]["src/app/model.py"]
    (run_copy.repo / "src/app/model.py").write_text("changed again\n")
    assert fingerprint(run_copy.repo)["files"]["src/app/model.py"] != first
    (run_copy.repo / "src/app/model.py").unlink()
    assert fingerprint(run_copy.repo)["files"] == {"src/app/model.py": None}


def test_failed_run_is_saved_as_failed(setup):
    run_copy = new_copy(setup)
    model = ScriptedModelClient([tool_reply("finish", summary="done")], repeat_last=True)
    run_task(task=TASK, task_path="t", config=load_config(), project=setup[3], model=model,
             model_name="m", run_copy=run_copy, sandbox_factory=FixAwareSandbox)
    session = Session.load(run_copy.run_dir)
    assert session.status == FAILED
    assert session.data["stop_reason"] == "checks still failing after the last fix round"
    assert session.data["counters"]["fix_rounds"] == 4
