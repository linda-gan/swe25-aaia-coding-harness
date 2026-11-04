"""Tests for cancellation: Ctrl+C or `cancel` stop a run at a safe point.

Handout test "Cancellation": one representative cancellation during a model
request or command; stop further actions or writes and clean up any child
processes. Both are tested:
- during a command, across processes: the harness runs in a child process,
  its check is stuck, and `python -m harness cancel` stops it from outside.
  A small fake `docker` stands in for Docker (it starts a process tree in its
  own process group, and `rm --force` kills that group, as Docker removes a
  container with all its processes). With Docker present, the same is also
  tested with a real container;
- during a model request, in-process.
"""
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from harness.cancellation import (
    Cancelled, CancelToken, handle_signals, lock_holder,
)
from harness.cli import main
from harness.config import PROJECT_ROOT, Task, load_config
from harness.controller import CANCELLED
from harness.model import ScriptedModelClient, tool_reply
from harness.runner import run_task
from harness.sandbox import CheckResult
from harness.session import CANCELLED as SESSION_CANCELLED, Session, SessionError
from harness.task_record import TaskRecord
from harness.workspace import create_run_copy, git

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
TASK = Task("Reject quantities.", ["src/app/model.py"], ["unit_tests"], ["unit_tests"],
            project="cosmicpython")
EDIT_A = tool_reply("edit_file", path="src/app/model.py",
                    old="return qty <= 10", new="return 0 < qty <= 10")


@pytest.fixture
def setup(tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "src" / "app").mkdir(parents=True)
    (pristine / "src" / "app" / "model.py").write_text(ORIGINAL)
    subprocess.run(["git", "init", "-q", str(pristine)], check=True)
    subprocess.run(["git", "-C", str(pristine), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(pristine), "-c", "user.name=T", "-c", "user.email=t@e.com",
                    "commit", "-q", "-m", "start"], check=True)
    commit = git(pristine, "rev-parse", "HEAD").strip()
    project = replace(load_config().project("cosmicpython"), commit=commit)
    run_copy = create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")
    return run_copy, project, tmp_path / "runs"


class PassingSandbox:
    def __init__(self, *args, **kwargs):
        pass

    def run_check(self, name):
        return CheckResult(name, ["pytest"], 0, "1 passed", False, 0.1, "fake")


# ---- the cancel token ---------------------------------------------------------------

def test_a_cancel_outside_a_wait_is_noted_and_stops_at_the_next_safe_point():
    token = CancelToken()
    token.request("Ctrl+C")          # e.g. while a file is being edited: nothing raised
    assert token.cancelled
    with pytest.raises(Cancelled, match="Ctrl\\+C"):
        token.check()                # the next safe point


def test_a_cancel_during_a_wait_breaks_it_off():
    token = CancelToken()
    with pytest.raises(Cancelled):
        with token.interruptible():
            token.request("cancel command")


def test_a_second_request_stops_at_once_anywhere():
    token = CancelToken()
    token.request("Ctrl+C")
    with pytest.raises(Cancelled):
        token.request("Ctrl+C")      # pressing Ctrl+C again


def test_signals_reach_the_token_and_handlers_are_restored():
    token = CancelToken()
    before = signal.getsignal(signal.SIGTERM)
    with handle_signals(token):
        with pytest.raises(Cancelled, match="cancel command"):
            with token.interruptible():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)        # broken off by the signal
    assert signal.getsignal(signal.SIGTERM) is before


def test_lock_holder_comes_from_the_kernel(tmp_path):
    import fcntl
    lock = tmp_path / "session.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        assert lock_holder(lock) is None
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert lock_holder(lock) == os.getpid()
    finally:
        os.close(fd)
    assert lock_holder(lock) is None


# ---- cancelling in-process ------------------------------------------------------------

def test_cancel_during_a_model_request_stops_at_once(setup):
    run_copy, project, runs = setup

    class SlowModel(ScriptedModelClient):
        """The first request works; during the second one, a cancel arrives."""

        def next_reply(self, messages, tools):
            if len(self.received) == 1:
                self.received.append(messages)
                os.kill(os.getpid(), signal.SIGTERM)  # as `cancel` would
                time.sleep(30)                        # a model that takes long
            return super().next_reply(messages, tools)

    started = time.monotonic()
    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=SlowModel([EDIT_A, EDIT_A]), model_name="m", run_copy=run_copy,
                      sandbox_factory=PassingSandbox)
    assert time.monotonic() - started < 10  # did not wait for the model
    assert report.cancelled and report.agent.stop_detail == "cancel command"
    assert report.verification is None and not report.passed
    assert report.agent.actions == 1  # only the edit before the cancel
    assert report.agent.interrupted_action is None  # a model request is not an action
    session = Session.load(run_copy.run_dir)
    assert session.status == SESSION_CANCELLED and session.data["cancel_reason"] == "cancel command"
    assert session.uncertain_action is None
    record = TaskRecord.load(run_copy.run_dir / "task.json")
    assert record.data["result"]["stop_reason"] == CANCELLED
    assert record.data["result"]["passed"] is None  # not verified, so neither passed nor failed


def test_cancel_between_steps_lets_the_current_step_finish_and_runs_nothing_more(setup):
    run_copy, project, runs = setup

    def on_event(event):
        if event.kind == "result":  # the edit has just completed: a cancel arrives
            os.kill(os.getpid(), signal.SIGINT)  # Ctrl+C

    model = ScriptedModelClient([EDIT_A, tool_reply("read_file", path="src/app/model.py"),
                                 tool_reply("finish", summary="done")])
    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=model, model_name="m", run_copy=run_copy, on_event=on_event,
                      sandbox_factory=PassingSandbox)
    assert report.cancelled and report.agent.stop_detail == "Ctrl+C"
    assert report.agent.actions == 1 and len(model.received) == 1  # no further request
    assert "0 < qty" in (run_copy.repo / "src/app/model.py").read_text()  # edit complete
    assert report.agent.interrupted_action is None  # stopped between steps


def test_cancel_during_final_verification_is_saved(setup):
    run_copy, project, runs = setup

    class SlowFinalCheck(PassingSandbox):
        calls = 0

        def run_check(self, name):
            SlowFinalCheck.calls += 1
            if SlowFinalCheck.calls == 2:  # 1st: at finish; 2nd: final verification
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(30)
            return super().run_check(name)

    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=ScriptedModelClient([EDIT_A, tool_reply("finish", summary="done")]),
                      model_name="m", run_copy=run_copy, sandbox_factory=SlowFinalCheck)
    assert report.cancelled and report.verification is None
    session = Session.load(run_copy.run_dir)
    assert session.status == SESSION_CANCELLED
    assert session.uncertain_action["tool"] == "final verification"


# ---- cancelling a stuck command from another process ------------------------------------

FAKE_DOCKER = textwrap.dedent('''\
    #!/usr/bin/env bash
    # Stands in for docker: "start" runs a process tree in its own process group,
    # "rm --force" kills that whole group, as removing a container kills its processes.
    STATE="__STATE__"
    cmd="$1"; shift
    case "$cmd" in
      create)
        while [ $# -gt 0 ]; do [ "$1" = "--name" ] && name="$2"; shift; done
        echo "$name" >> "$STATE/created"; exit 0 ;;
      start)
        name="${@: -1}"
        setsid sh -c 'sleep 300 & sleep 300' &
        echo $! > "$STATE/$name.pid"
        wait ;;
      rm)
        name="${@: -1}"
        [ -f "$STATE/$name.pid" ] && kill -9 -- -"$(cat "$STATE/$name.pid")" 2>/dev/null
        echo "$name" >> "$STATE/removed"; exit 0 ;;
    esac
''')

CHILD = textwrap.dedent('''
    import functools, sys
    from dataclasses import replace
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from harness.config import Task, load_config
    from harness.model import ScriptedModelClient, tool_reply
    from harness.runner import run_task
    from harness.sandbox import DockerSandbox
    from harness.workspace import RunCopy

    run_dir = Path(sys.argv[2])
    run_copy = RunCopy(run_dir.name, run_dir, run_dir / "repo")
    task = Task("Reject quantities.", ["src/app/model.py"], ["unit_tests"], ["unit_tests"],
                project="cosmicpython")
    project = replace(load_config().project("cosmicpython"), commit=sys.argv[3])
    model = ScriptedModelClient([
        tool_reply("edit_file", path="src/app/model.py",
                   old="return qty <= 10", new="return 0 < qty <= 10"),
        tool_reply("run_check", name="unit_tests"),          # gets stuck
        tool_reply("edit_file", path="src/app/model.py",     # must never run
                   old="0 < qty", new="AFTER_CANCEL"),
    ])
    report = run_task(task=task, task_path="t", config=load_config(), project=project,
                      model=model, model_name="m", run_copy=run_copy,
                      sandbox_factory=functools.partial(DockerSandbox, docker=sys.argv[4]))
    print("STOP:", report.agent.stop_reason)
''')


def group_alive(pgid: int) -> bool:
    """True if any process still belongs to this process group."""
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z" and int(fields[2]) == pgid:  # state, ppid, pgrp
            return True
    return False


def test_cancel_command_stops_a_stuck_check_and_its_child_processes(setup, tmp_path, capsys):
    run_copy, project, runs = setup
    state = tmp_path / "docker-state"
    state.mkdir()
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER.replace("__STATE__", str(state)))
    fake.chmod(0o755)
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    child = subprocess.Popen([sys.executable, str(script), str(PROJECT_ROOT),
                              str(run_copy.run_dir), project.commit, str(fake)],
                             stdout=subprocess.PIPE, text=True)
    try:
        # Wait until the check is running: its process group exists.
        deadline = time.monotonic() + 30
        pid_files = []
        while time.monotonic() < deadline and not pid_files:
            pid_files = list(state.glob("*.pid"))
            time.sleep(0.1)
        assert pid_files, "the check never started"
        pgid = int(pid_files[0].read_text())
        assert group_alive(pgid)

        # From "another terminal": cancel the session.
        assert main(["cancel", run_copy.run_id[-6:], "--runs-dir", str(runs), "--wait", "30"]) == 0
        out, _ = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()

    assert "STOP: cancelled" in out
    assert "Stopped. Status: cancelled" in capsys.readouterr().out
    # Child processes cleaned up: the container was removed and its processes are gone.
    assert (state / "removed").read_text().split() == (state / "created").read_text().split()
    deadline = time.monotonic() + 5
    while group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not group_alive(pgid)
    # No further actions or writes: the edit before the check stayed, the one after never ran.
    text = (run_copy.repo / "src/app/model.py").read_text()
    assert "0 < qty" in text and "AFTER_CANCEL" not in text
    session = Session.load(run_copy.run_dir)
    assert session.status == SESSION_CANCELLED and session.data["cancel_reason"] == "cancel command"
    assert session.uncertain_action["tool"] == "run_check"
    assert session.data["counters"]["actions"] == 2


def test_cancel_refuses_a_session_that_is_not_running(setup, capsys):
    run_copy, project, runs = setup
    run_task(task=TASK, task_path="t", config=load_config(), project=project,
             model=ScriptedModelClient([tool_reply("finish", summary="done")]),
             model_name="m", run_copy=run_copy, sandbox_factory=PassingSandbox)
    assert main(["cancel", run_copy.run_id, "--runs-dir", str(runs)]) == 2
    assert "is not running (status: passed)" in capsys.readouterr().err


def test_cancel_does_not_trust_the_pid_in_the_session_file(setup):
    run_copy, project, runs = setup
    session = Session.create(run_copy, "t", project, "m")
    try:
        session.data["pid"] = 1  # a changed file must not make cancel signal another process
        session.save()
        reopened = Session.load(run_copy.run_dir)
        # The real lock holder is this test process, which cancel refuses to signal.
        with pytest.raises(SessionError, match="cannot cancel itself"):
            reopened.request_cancel()
    finally:
        session.close()


# ---- with real Docker ------------------------------------------------------------------

def _docker_with_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _docker_with_image("python:3.12-slim"),
                    reason="needs Docker running and the python:3.12-slim image")
def test_cancel_removes_a_real_container_with_its_processes(setup):
    run_copy, project, runs = setup
    project = replace(project, sandbox=replace(project.sandbox, image="python:3.12-slim"),
                      checks={"unit_tests": ["sh", "-c", "sleep 300 & sleep 300"]})
    before = set(subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout.split())
    threading.Timer(4, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=ScriptedModelClient([tool_reply("run_check", name="unit_tests")]),
                      model_name="m", run_copy=run_copy)
    assert report.cancelled
    after = set(subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout.split())
    assert after <= before  # the check's container is gone
