"""Tests for safe resume.

Handout test "Safe resume": resume against the saved working-copy state,
including the harness's edits; recheck permissions and limits; pause on
mismatches or uncertain actions.
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

from harness.cli import main
from harness.config import PROJECT_ROOT, load_config
from harness.controller import ACTION_LIMIT, CANCELLED, FINISHED
from harness.model import ModelReply, ScriptedModelClient, ToolCall, tool_reply
from harness.runner import resume_task, run_task
from harness.sandbox import CheckResult
from harness.session import PAUSED, PASSED, SESSION_FILE, Session, SessionStore
from harness.task_record import TaskRecord
from harness.workspace import create_run_copy, git

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
EDIT_A = tool_reply("edit_file", path="src/app/model.py",
                    old="return qty <= 10", new="return 0 < qty <= 10")
READ = tool_reply("read_file", path="src/app/model.py")
FINISH = tool_reply("finish", summary="done")
TASK_TOML = '''
project = "cosmicpython"
description = "Reject quantities of zero or less."
editable_files = ["src/app/model.py"]
agent_checks = ["unit_tests"]
final_checks = ["unit_tests"]
'''


class CountingSandbox:
    """Passes once the fix is in. Counts every check, to prove what (did not) run."""
    ran = []

    def __init__(self, repo, *args, **kwargs):
        self.repo = repo

    def run_check(self, name):
        CountingSandbox.ran.append(name)
        fixed = "0 < qty" in (self.repo / "src/app/model.py").read_text()
        return CheckResult(name, ["pytest"], 0 if fixed else 1, "ok" if fixed else "1 failed",
                           False, 0.1, "fake")


class Env:
    """A small project, its config, a task file and a runs folder."""

    def __init__(self, tmp_path, max_actions=None):
        pristine = tmp_path / "pristine"
        (pristine / "src" / "app").mkdir(parents=True)
        (pristine / "src" / "app" / "model.py").write_text(ORIGINAL)
        subprocess.run(["git", "init", "-q", str(pristine)], check=True)
        subprocess.run(["git", "-C", str(pristine), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(pristine), "-c", "user.name=T", "-c",
                        "user.email=t@e.com", "commit", "-q", "-m", "start"], check=True)
        self.commit = git(pristine, "rev-parse", "HEAD").strip()
        self.pristine, self.runs = pristine, tmp_path / "runs"
        self.task_file = tmp_path / "task.toml"
        self.task_file.write_text(TASK_TOML + (f"max_actions = {max_actions}\n" if max_actions else ""))
        base = load_config()
        self.project = replace(base.project("cosmicpython"), commit=self.commit)
        self.config = replace(base, projects={"cosmicpython": self.project})
        CountingSandbox.ran = []

    def task(self):
        from harness.config import load_task
        return load_task(self.task_file)

    def start(self, replies, on_event=None):
        """A run that gets cancelled (Ctrl+C) right after its first completed edit."""
        run_copy = create_run_copy(self.commit, pristine=self.pristine, runs_dir=self.runs)

        def cancel_after_edit(event):
            if event.kind == "result" and event.message.startswith("Edited"):
                os.kill(os.getpid(), signal.SIGINT)
            if on_event:
                on_event(event)

        report = run_task(task=self.task(), task_path=self.task_file, config=self.config,
                          project=self.project, model=ScriptedModelClient(replies),
                          model_name="scripted", run_copy=run_copy,
                          on_event=cancel_after_edit, sandbox_factory=CountingSandbox)
        assert report.cancelled
        return run_copy

    def resume(self, run_copy, replies, accept=False):
        model = ScriptedModelClient(replies)
        session = SessionStore(self.runs).open(run_copy.run_id)
        outcome = resume_task(session, self.config, model, "scripted",
                              accept_current_state=accept, sandbox_factory=CountingSandbox)
        return outcome, model


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---- resuming from the checkpoint --------------------------------------------------

def test_resume_continues_from_the_checkpoint_with_the_harness_edit(env):
    run_copy = env.start([READ, EDIT_A, READ])           # cancelled after the edit (step 2)
    outcome, model = env.resume(run_copy, [FINISH])

    assert outcome.report and outcome.report.agent.stop_reason == FINISHED
    assert outcome.report.passed and outcome.check.blocking == outcome.check.decisions == []
    assert any("including the harness's own edits" in p for p in outcome.check.passed)
    # The harness's edit from before the cancel is still there and counted.
    assert "0 < qty" in (run_copy.repo / "src/app/model.py").read_text()
    session = Session.load(run_copy.run_dir)
    assert session.status == PASSED
    assert session.data["counters"]["actions"] == 2          # used limits carried over
    assert session.data["resumes"][0]["from_status"] == "cancelled"
    # The model continued the saved conversation, with the harness's note at the end.
    sent = model.received[0]
    assert sent[1]["content"] == "Reject quantities of zero or less."
    note = sent[-1]
    assert note["role"] == "user" and "the user has now resumed it" in note["content"]
    assert "Limits left: 28 actions" in note["content"]
    record = TaskRecord.load(run_copy.run_dir / "task.json")
    assert [s["step"] for s in record.steps] == [1, 2, 3]   # numbering continues
    assert record.data["earlier_results"][0]["stop_reason"] == CANCELLED
    assert record.data["result"]["stop_reason"] == FINISHED


def test_used_limits_are_kept(tmp_path):
    env = Env(tmp_path, max_actions=3)
    run_copy = env.start([READ, EDIT_A, READ])            # 2 of 3 actions used
    outcome, _ = env.resume(run_copy, [READ, READ, READ], accept=False)
    assert outcome.report.agent.stop_reason == ACTION_LIMIT
    assert outcome.report.agent.actions == 3              # only one more was allowed


def test_resume_after_restart_reads_only_files(env):
    run_copy = env.start([READ, EDIT_A, READ])
    # A separate process checks the session as it would be after a restart.
    out = subprocess.run([sys.executable, "-m", "harness", "show", run_copy.run_id,
                          "--runs-dir", str(env.runs)], cwd=PROJECT_ROOT, capture_output=True,
                         text=True, env={**os.environ, "NO_COLOR": "1"}, timeout=60)
    assert "Status:   cancelled" in out.stdout
    outcome, _ = env.resume(run_copy, [FINISH])
    assert outcome.report.passed


# ---- pausing on mismatches ------------------------------------------------------------

def assert_paused_and_nothing_ran(outcome, model, run_copy, words):
    assert outcome.report is None
    assert model.received == []          # no model request
    assert CountingSandbox.ran == []     # no check
    session = Session.load(run_copy.run_dir)
    assert session.status == PAUSED
    assert any(words in reason for reason in session.data["pause_reasons"])


def test_a_file_changed_outside_the_harness_pauses(env):
    run_copy = env.start([READ, EDIT_A, READ])
    (run_copy.repo / "src/app/model.py").write_text("def can_allocate(qty):\n    return True\n")
    outcome, model = env.resume(run_copy, [FINISH])
    assert_paused_and_nothing_ran(outcome, model, run_copy,
                                  "src/app/model.py (changed since the checkpoint)")
    assert outcome.check.decisions and not outcome.check.blocking


def test_an_undone_harness_edit_pauses(env):
    run_copy = env.start([READ, EDIT_A, READ])
    git(run_copy.repo, "checkout", "--", "src/app/model.py")
    outcome, model = env.resume(run_copy, [FINISH])
    assert_paused_and_nothing_ran(outcome, model, run_copy, "src/app/model.py (change undone)")


def test_accepting_the_current_state_continues_and_tells_the_model(env):
    run_copy = env.start([READ, EDIT_A, READ])
    (run_copy.repo / "src/app/model.py").write_text(
        "def can_allocate(qty):\n    return 0 < qty <= 10  # reviewed\n")
    env.resume(run_copy, [FINISH])                       # pauses first
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report.passed
    note = model.received[0][-1]["content"]
    assert "differs from the last checkpoint in: src/app/model.py" in note
    resumes = Session.load(run_copy.run_dir).data["resumes"]
    assert resumes[-1]["from_status"] == PAUSED and resumes[-1]["accepted"]


# ---- permissions and limits are checked again -----------------------------------------

@pytest.mark.parametrize("change, words", [
    (lambda t: t.replace('editable_files = ["src/app/model.py"]',
                         'editable_files = ["src/app/model.py", "src/app/other.py"]'),
     "allowed_changes"),
    (lambda t: t.replace('agent_checks = ["unit_tests"]', 'agent_checks = []'), "agent checks"),
    (lambda t: t.replace("Reject quantities", "Accept all quantities"), "request"),
    (lambda t: t + "max_actions = 50\n", "limits changed"),
])
def test_a_changed_task_blocks_even_with_acceptance(env, change, words):
    run_copy = env.start([READ, EDIT_A, READ])
    env.task_file.write_text(change(env.task_file.read_text()))
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report is None and model.received == []
    assert any(words in item for item in outcome.check.blocking)


def test_a_project_no_longer_allowed_blocks(env):
    run_copy = env.start([READ, EDIT_A, READ])
    env.config = replace(env.config, projects={"other": replace(env.project, name="other")})
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report is None and "not allowed" in outcome.check.blocking[0]


def test_changed_check_commands_block(env):
    run_copy = env.start([READ, EDIT_A, READ])
    changed = replace(env.project, checks={**env.project.checks, "unit_tests": ["pytest", "-x"]})
    env.config = replace(env.config, projects={"cosmicpython": changed})
    outcome, _ = env.resume(run_copy, [FINISH], accept=True)
    assert any("check commands" in item for item in outcome.check.blocking)


def test_used_up_limits_block(tmp_path):
    env = Env(tmp_path, max_actions=2)
    run_copy = env.start([READ, EDIT_A, READ])           # stopped at 2 of 2
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report is None and model.received == []
    assert any("Limits used up: actions (2 of 2)" in item for item in outcome.check.blocking)


@pytest.mark.parametrize("status_words", ["passed"])
def test_finished_sessions_cannot_be_resumed(env, status_words):
    run_copy = env.start([READ, EDIT_A, READ])
    env.resume(run_copy, [FINISH])                       # now passed
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report is None and model.received == []
    assert "only cancelled, interrupted or paused" in outcome.check.blocking[0]
    assert Session.load(run_copy.run_dir).status == PASSED  # not turned into paused


def test_a_running_session_cannot_be_resumed(env):
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)
    session = Session.create(run_copy, env.task_file, env.project, "m")
    try:
        outcome, _ = env.resume(run_copy, [FINISH], accept=True)
        assert "The session is running" in outcome.check.blocking[0]
    finally:
        session.close()


# ---- uncertain actions ----------------------------------------------------------------

def cancel_during_check(env, replies):
    """Cancel while a check runs: the check becomes the uncertain action."""
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)

    class StuckOnce(CountingSandbox):
        def run_check(self, name):
            if not CountingSandbox.ran:
                CountingSandbox.ran.append(name)
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(30)
            return super().run_check(name)

    run_task(task=env.task(), task_path=env.task_file, config=env.config, project=env.project,
             model=ScriptedModelClient(replies), model_name="scripted", run_copy=run_copy,
             sandbox_factory=StuckOnce)
    CountingSandbox.ran = []
    return run_copy


def test_an_uncertain_check_pauses_and_is_never_replayed(env):
    run_copy = cancel_during_check(env, [EDIT_A, tool_reply("run_check", name="unit_tests")])
    outcome, model = env.resume(run_copy, [FINISH])
    assert outcome.report is None and model.received == [] and CountingSandbox.ran == []
    assert any("Step 2 (run_check unit_tests) was cut off" in d for d in outcome.check.decisions)

    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report.passed
    # The check was not repeated by the harness: the only checks are the ones at
    # finish and in the final verification.
    assert CountingSandbox.ran == ["unit_tests", "unit_tests"]
    sent = model.received[0]
    interrupted = sent[-2]
    assert interrupted["role"] == "tool"
    assert "Interrupted: the run stopped while this call ran" in interrupted["content"]
    assert "was not repeated" in sent[-1]["content"]


def test_calls_that_never_ran_get_a_result(env):
    two_calls = ModelReply(tool_calls=[ToolCall("run_check", {"name": "unit_tests"}),
                                       ToolCall("read_file", {"path": "src/app/model.py"})])
    run_copy = cancel_during_check(env, [EDIT_A, two_calls])
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    sent = model.received[0]
    assert "Interrupted:" in sent[-3]["content"]           # run_check, cut off
    assert "Not run: the run stopped before this call." in sent[-2]["content"]  # read_file


def test_an_uncertain_read_only_action_does_not_pause(env):
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)
    session = Session.create(run_copy, env.task_file, env.project, "scripted")
    TaskRecord.create(run_copy, env.task(), env.task_file, env.project, env.config.limits)
    model_messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    session.data.update({"messages": model_messages,
                         "context": {"nonce": "abcd1234", "stubbed": []}})
    session.pending({"step": 1, "action": 1, "tool": "read_file", "target": "src/app/model.py"})
    session.close()                                       # process "gone": interrupted
    outcome, model = env.resume(run_copy, [FINISH])
    assert outcome.report is not None and not outcome.check.decisions
    assert any("It only reads, so nothing changed" in n for n in outcome.check.notes)


CHILD = textwrap.dedent('''
    import sys, time
    from dataclasses import replace
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from harness.config import load_config, load_task
    from harness.model import ScriptedModelClient, tool_reply
    from harness.runner import run_task
    from harness.workspace import RunCopy

    run_dir = Path(sys.argv[2])
    base = load_config()
    project = replace(base.project("cosmicpython"), commit=sys.argv[3])
    config = replace(base, projects={"cosmicpython": project})

    class Stuck:
        def __init__(self, *a, **k):
            pass
        def run_check(self, name):
            time.sleep(120)

    run_task(task=load_task(sys.argv[4]), task_path=sys.argv[4], config=config, project=project,
             model=ScriptedModelClient([
                 tool_reply("edit_file", path="src/app/model.py",
                            old="return qty <= 10", new="return 0 < qty <= 10"),
                 tool_reply("run_check", name="unit_tests")]),
             model_name="scripted", run_copy=RunCopy(run_dir.name, run_dir, run_dir / "repo"),
             sandbox_factory=Stuck)
''')


def test_resume_after_the_harness_process_was_killed(env, tmp_path):
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    child = subprocess.Popen([sys.executable, str(script), str(PROJECT_ROOT),
                              str(run_copy.run_dir), env.commit, str(env.task_file)])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            path = run_copy.run_dir / SESSION_FILE
            if path.exists() and (json.loads(path.read_text()).get("pending_action") or {}).get(
                    "tool") == "run_check":
                break
            time.sleep(0.1)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()

    outcome, model = env.resume(run_copy, [FINISH])       # interrupted + uncertain check
    assert outcome.report is None and "run_check" in outcome.check.decisions[0]
    paused = Session.load(run_copy.run_dir)
    assert paused.status == PAUSED and paused.data["paused_from"] == "interrupted"
    assert paused.uncertain_action["tool"] == "run_check"  # kept while paused
    outcome, model = env.resume(run_copy, [FINISH], accept=True)
    assert outcome.report.passed
    resumed = Session.load(run_copy.run_dir).data["resumes"][0]
    assert resumed["from_status"] == PAUSED and resumed["paused_from"] == "interrupted"
    assert "its process ended unexpectedly" in model.received[0][-1]["content"]


def test_cancelled_during_verification_resumes_with_verification_only(env):
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)

    class StopInVerification(CountingSandbox):
        def run_check(self, name):
            CountingSandbox.ran.append(name)
            if len(CountingSandbox.ran) == 2:              # 1: at finish, 2: verification
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(30)
            return CheckResult(name, ["pytest"], 0, "ok", False, 0.1, "fake")

    run_task(task=env.task(), task_path=env.task_file, config=env.config, project=env.project,
             model=ScriptedModelClient([EDIT_A, FINISH]), model_name="scripted",
             run_copy=run_copy, sandbox_factory=StopInVerification)
    CountingSandbox.ran = []
    outcome, model = env.resume(run_copy, [], accept=True)
    assert outcome.report.passed and model.received == []   # no model request: only verification
    assert CountingSandbox.ran == ["unit_tests"]


# ---- the command line -----------------------------------------------------------------

def test_cli_resume_shows_the_checks_and_pauses_with_exit_code_3(env, capsys, monkeypatch):
    monkeypatch.setattr("harness.cli.load_config", lambda: env.config)  # the test project
    run_copy = env.start([READ, EDIT_A, READ])
    (run_copy.repo / "src/app/model.py").write_text("changed outside\n")
    code = main(["resume", run_copy.run_id[-6:], "--runs-dir", str(env.runs)])
    out = capsys.readouterr().out
    assert code == 3
    assert "Checks before resuming" in out and "DECIDE" in out
    assert "--accept-current-state" in out
    assert main(["sessions", "--runs-dir", str(env.runs)]) == 0
    assert "paused" in capsys.readouterr().out
    assert main(["show", run_copy.run_id, "--runs-dir", str(env.runs)]) == 0
    assert "Paused, because:" in capsys.readouterr().out
