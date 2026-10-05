"""Tests for a complete run: agent, verification, run record and CLI output.

Uses a scripted model, a fake sandbox and a small real Git repository, so no
Ollama or Docker is needed. Covers the handout tests "Failed command" (the
run never claims checks passed) and "Bug fix" (the acceptance check fails on
the starting code and passes after the change).
"""
import json
import subprocess

import pytest

from harness.cli import Style, format_result, format_verification
from harness.config import load_config, Task
from harness.controller import FINISHED
from harness.model import ScriptedModelClient, tool_reply
from harness.runner import check_task, run_task
from harness.sandbox import CheckResult
from harness.verification import verify
from harness.workspace import create_run_copy, git

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
FIXED = "def can_allocate(qty):\n    return 0 < qty <= 10\n"

TASK = Task(
    description="Reject quantities of zero or less.",
    editable_files=["src/app/model.py"],
    agent_checks=["unit_tests"],
    final_checks=["acceptance", "unit_tests"],
)


class FakeSandbox:
    """Acts like the Docker sandbox. The acceptance check passes only once the
    file contains the fix, like the real acceptance test would."""

    def __init__(self, repo, *args, **kwargs):
        self.repo = repo
        self.ran = []

    def run_check(self, name):
        self.ran.append(name)
        if name == "acceptance":
            fixed = "0 < qty" in (self.repo / "src/app/model.py").read_text()
            code, out = (0, "3 passed") if fixed else (1, "FAILED test_rejects\n2 failed, 1 passed")
        else:
            code, out = 0, "20 passed"
        return CheckResult(name, ["pytest"], code, out, False, 0.1, "fake")


class UnavailableSandbox(FakeSandbox):
    def run_check(self, name):
        return CheckResult(name, ["pytest"], None, "", False, 0.0, "fake",
                           error="Docker is not installed or not on PATH ('docker').")


@pytest.fixture
def run_copy(tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "src" / "app").mkdir(parents=True)
    (pristine / "src" / "app" / "model.py").write_text(ORIGINAL)
    subprocess.run(["git", "init", "-q", str(pristine)], check=True)
    subprocess.run(["git", "-C", str(pristine), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(pristine), "-c", "user.name=T", "-c", "user.email=t@e.com",
                    "commit", "-q", "-m", "start"], check=True)
    commit = git(pristine, "rev-parse", "HEAD").strip()
    return create_run_copy(commit, pristine=pristine, runs_dir=tmp_path / "runs")


@pytest.fixture
def config():
    return load_config()


def run(run_copy, config, replies, sandbox_factory=FakeSandbox):
    return run_task(
        task=TASK, task_path="tasks/test.toml", config=config,
        model=ScriptedModelClient(replies), model_name="scripted",
        run_copy=run_copy, sandbox_factory=sandbox_factory,
    )


FIX_SCRIPT = [
    tool_reply("read_file", path="src/app/model.py"),
    tool_reply("edit_file", path="src/app/model.py", old="return qty <= 10", new="return 0 < qty <= 10"),
    tool_reply("finish", summary="Quantities must be positive."),
]


# ---- bug fix: failing check becomes passing -------------------------------

def test_acceptance_check_fails_on_starting_code(run_copy):
    report = verify(FakeSandbox(run_copy.repo), run_copy.repo, TASK)
    assert not report.passed
    assert report.checks[0].status == "failed"


def test_fix_makes_acceptance_check_pass_and_existing_tests_still_pass(run_copy, config):
    report = run(run_copy, config, FIX_SCRIPT)

    assert report.agent.stop_reason == FINISHED
    assert report.passed
    assert [c.status for c in report.verification.checks] == ["passed", "passed"]
    assert report.verification.changed_files == [("M", "src/app/model.py")]
    assert "+    return 0 < qty <= 10" in report.verification.diff


# ---- failed checks are never reported as passing --------------------------

def test_model_says_done_without_fixing_and_run_is_failed(run_copy, config):
    report = run(run_copy, config, [tool_reply("finish", summary="All done!")])

    assert report.agent.finished  # the model claimed success
    assert not report.passed  # the harness disagrees
    text = format_verification(Style(False), report.verification) + format_result(Style(False), report)
    assert "acceptance   FAILED" in text
    assert "2 failed, 1 passed" in text  # the failing output stays visible
    assert "the model reported the task as done, but the checks did not pass" in text
    assert "RESULT: FAILED" in text


def test_unavailable_checks_make_the_run_fail(run_copy, config):
    report = run(run_copy, config, FIX_SCRIPT, sandbox_factory=UnavailableSandbox)
    assert not report.passed
    text = format_verification(Style(False), report.verification)
    assert "UNAVAILABLE" in text
    assert "Docker is not installed" in text


def test_verification_runs_even_when_the_agent_hits_a_limit(run_copy, config):
    sandbox_holder = {}

    def factory(*args, **kwargs):
        sandbox_holder["s"] = FakeSandbox(*args, **kwargs)
        return sandbox_holder["s"]

    model = ScriptedModelClient([tool_reply("list_files")], repeat_last=True)
    report = run_task(task=TASK, task_path="t", config=config, model=model, model_name="s",
                      run_copy=run_copy, sandbox_factory=factory)
    assert report.agent.stop_reason == "action limit reached"
    assert sandbox_holder["s"].ran == ["acceptance", "unit_tests"]
    assert not report.passed


def test_file_changed_outside_the_task_fails_the_scope_check(run_copy):
    (run_copy.repo / "src/app/model.py").write_text(FIXED)
    (run_copy.repo / "planted.py").write_text("print('hi')\n")  # e.g. written by a test run
    report = verify(FakeSandbox(run_copy.repo), run_copy.repo, TASK)

    assert all(c.passed for c in report.checks)
    assert not report.scope.passed
    assert report.scope.outside == ["planted.py"]
    assert not report.passed


# ---- run record -----------------------------------------------------------

def test_run_record_is_saved(run_copy, config):
    report = run(run_copy, config, FIX_SCRIPT)
    record = json.loads((run_copy.run_dir / "run.json").read_text())

    assert record["passed"] is True
    assert record["agent"]["stop_reason"] == FINISHED
    assert record["agent"]["actions"] == 2
    assert [c["status"] for c in record["verification"]["checks"]] == ["passed", "passed"]
    assert (run_copy.run_dir / "changes.diff").read_text() == report.verification.diff

    timing = record["timing"]
    assert timing["started_at"] <= timing["finished_at"]
    assert timing["total_seconds"] >= timing["agent_seconds"]
    assert "elapsed_seconds" in record["agent"]["events"][0]


def test_result_shows_timing(run_copy, config):
    report = run(run_copy, config, FIX_SCRIPT)
    assert "Time: agent" in format_result(Style(False), report)


def test_task_with_unknown_check_is_refused_early(config):
    bad = Task("x", ["a.py"], ["unit_tests"], ["integration"])
    with pytest.raises(ValueError, match="'integration'"):
        check_task(bad, config)
