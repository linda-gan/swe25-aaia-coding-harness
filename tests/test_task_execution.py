"""Tests for task execution: the task record, the step log, the edit-test-fix
loop at finish, and per-task action limits.

Handout test "Checks and loop": failed-check handling with scripted model
replies; failed or unavailable checks never appear as passed.
"""
import json
import subprocess
from dataclasses import replace

import pytest

from harness.cli import main
from harness.config import (
    ACCEPTANCE_ROOT, PROJECT_ROOT, LimitsConfig, Task, load_config, load_task,
)
from harness.controller import (
    ACTION_LIMIT, CHECKS_FAILING, CHECKS_UNAVAILABLE, FINISHED, AgentController,
)
from harness.model import ModelReply, ScriptedModelClient, ToolCall, tool_reply
from harness.repo_tools import RepositoryTools
from harness.runner import effective_limits, run_task
from harness.sandbox import CheckResult
from harness.task_record import TaskRecord
from harness.workspace import WorkspaceError, create_run_copy, git

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
TASK = Task(
    description="Reject quantities of zero or less.",
    editable_files=["src/app/model.py"],
    agent_checks=["unit_tests"],
    final_checks=["acceptance", "unit_tests"],
    acceptance_dir=ACCEPTANCE_ROOT / "invalid_quantity",  # not read by the fake sandbox
    project="cosmicpython",
)
LIMITS = LimitsConfig(max_actions=10, max_denied_actions=3, max_retries=1,
                      max_tool_output_chars=2000, max_fix_rounds=3)
FIX = tool_reply("edit_file", path="src/app/model.py",
                 old="return qty <= 10", new="return 0 < qty <= 10")


class FixAwareSandbox:
    """Checks pass only once the fix is in the file. Records what ran."""

    def __init__(self, repo, *args, **kwargs):
        self.repo = repo
        self.ran = []

    def run_check(self, name):
        self.ran.append(name)
        fixed = "0 < qty" in (self.repo / "src/app/model.py").read_text()
        code, out = (0, "3 passed") if fixed else (1, "FAILED test_rejects\n1 failed, 2 passed")
        return CheckResult(name, ["pytest"], code, out, False, 0.1, "fake")


class UnavailableSandbox(FixAwareSandbox):
    def run_check(self, name):
        self.ran.append(name)
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
def project(run_copy):
    """The real project's settings, pinned to the small repository's commit."""
    commit = git(run_copy.repo, "rev-parse", "HEAD").strip()
    return replace(load_config().project("cosmicpython"), commit=commit)


def controller(repo, replies, sandbox=None, task=TASK, limits=LIMITS, repeat_last=False):
    model = ScriptedModelClient(replies, repeat_last=repeat_last)
    sandbox = sandbox or FixAwareSandbox(repo)
    return AgentController(model, RepositoryTools(repo), sandbox, task, limits), model, sandbox


# ---- the task record ------------------------------------------------------------

def test_task_record_holds_request_commit_scope_checks_and_limits(run_copy, project):
    record = TaskRecord.create(run_copy, TASK, "tasks/test.toml", project, LIMITS)
    data = json.loads((run_copy.run_dir / "task.json").read_text())
    assert data == record.data
    assert data["request"] == TASK.description
    assert data["starting_commit"] == project.commit
    assert data["project"]["name"] == "cosmicpython"
    assert data["allowed_changes"] == ["src/app/model.py"]
    assert data["checks"]["agent"] == ["unit_tests"]
    assert data["checks"]["final"] == ["acceptance", "unit_tests"]
    assert data["checks"]["commands"]["acceptance"][:2] == ["pytest", "/acceptance"]
    assert data["acceptance_tests"] == "invalid_quantity"
    assert data["limits"]["max_actions"] == 10 and data["limits"]["max_fix_rounds"] == 3
    assert data["status"] == "running" and data["steps"] == []


def test_record_refuses_a_copy_that_is_not_at_the_pinned_commit(run_copy, project):
    with pytest.raises(WorkspaceError, match="not at the pinned commit"):
        TaskRecord.create(run_copy, TASK, "t", replace(project, commit="0" * 40), LIMITS)


def test_steps_are_saved_while_the_run_is_going(run_copy, project):
    path = run_copy.run_dir / "task.json"
    seen = []

    class PeekingModel(ScriptedModelClient):
        """Before each reply, read the record from disk, as another program would."""

        def next_reply(self, messages, tools):
            if path.exists():
                seen.append(len(json.loads(path.read_text())["steps"]))
            return super().next_reply(messages, tools)

    model = PeekingModel([tool_reply("read_file", path="src/app/model.py"), FIX,
                          tool_reply("finish", summary="done")])
    run_task(task=TASK, task_path="t", config=load_config(), project=project, model=model,
             model_name="m", run_copy=run_copy, sandbox_factory=FixAwareSandbox)
    assert seen == [0, 1, 2]  # each step was on disk before the next model request
    assert not list(run_copy.run_dir.glob(".*.tmp"))  # atomic writes leave nothing behind


def test_record_after_the_run_can_be_reopened(run_copy, project):
    model = ScriptedModelClient([tool_reply("read_file", path="src/app/model.py"), FIX,
                                 tool_reply("finish", summary="done")])
    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=model, model_name="m", run_copy=run_copy,
                      sandbox_factory=FixAwareSandbox)
    record = TaskRecord.load(run_copy.run_dir / "task.json")  # e.g. after a restart
    assert record.data["status"] == "stopped"
    assert record.data["result"]["stop_reason"] == FINISHED
    assert record.data["result"]["passed"] is report.passed is True
    assert [s["phase"] for s in record.steps] == ["exploring", "editing", "finish checks"]
    assert [s["tool"] for s in record.steps] == ["read_file", "edit_file", "finish"]
    assert record.steps[1]["target"] == "src/app/model.py"
    assert record.steps[2]["status"] == "passed"
    assert record.steps[2]["target"] == "unit_tests passed"
    run_json = json.loads((run_copy.run_dir / "run.json").read_text())
    assert run_json["agent"]["steps"] == 3 and run_json["agent"]["fix_rounds"] == 0


# ---- the edit-test-fix loop ---------------------------------------------------------

def test_finish_runs_the_agent_checks_without_counting_an_action(run_copy):
    ctl, _, sandbox = controller(run_copy.repo, [FIX, tool_reply("finish", summary="done")])
    run = ctl.run()
    assert run.stop_reason == FINISHED and run.finished
    assert sandbox.ran == ["unit_tests"]  # the harness ran it at finish
    assert run.actions == 1 and run.fix_rounds == 0
    assert [e.kind for e in run.events][-3:] == ["finish", "check", "stop"]


def test_failing_checks_at_finish_start_a_fix_round(run_copy):
    ctl, model, _ = controller(run_copy.repo, [
        tool_reply("finish", summary="done already"),  # too early: checks fail
        FIX,
        tool_reply("finish", summary="now done"),
    ])
    run = ctl.run()

    assert run.stop_reason == FINISHED and run.fix_rounds == 1
    assert run.summary == "now done"
    # What the model got after the failed finish: the check output, marked as
    # untrusted, and then a trusted note from the harness.
    *_, result, note = model.received[1]
    assert result["role"] == "tool" and result["content"].startswith("<<<untrusted ")
    assert "Check unit_tests: FAILED" in result["content"] and "1 failed" in result["content"]
    assert note["role"] == "user" and "<<<" not in note["content"]
    assert "Not finished yet" in note["content"] and "fix round 1 of 3" in note["content"]
    phases = [m["harness_step"]["phase"] for m in run.messages if "harness_step" in m]
    assert phases == ["finish checks", "fix round 1"]


def test_checks_that_never_pass_stop_after_the_last_fix_round(run_copy):
    ctl, model, sandbox = controller(run_copy.repo, [tool_reply("finish", summary="done")],
                                     repeat_last=True)
    run = ctl.run()
    assert run.stop_reason == CHECKS_FAILING and not run.finished
    assert run.fix_rounds == 4  # 3 fix rounds granted; the 4th failed finish stops
    assert len(model.received) == 4 and len(sandbox.ran) == 4  # nothing more was asked


def test_zero_fix_rounds_stop_at_the_first_failure(run_copy):
    ctl, model, _ = controller(run_copy.repo, [tool_reply("finish", summary="done")],
                               limits=replace(LIMITS, max_fix_rounds=0), repeat_last=True)
    run = ctl.run()
    assert run.stop_reason == CHECKS_FAILING and len(model.received) == 1


def test_unavailable_checks_stop_at_once_and_never_pass(run_copy, project):
    model = ScriptedModelClient([FIX, tool_reply("finish", summary="done")], repeat_last=True)
    report = run_task(task=TASK, task_path="t", config=load_config(), project=project,
                      model=model, model_name="m", run_copy=run_copy,
                      sandbox_factory=UnavailableSandbox)
    assert report.agent.stop_reason == CHECKS_UNAVAILABLE
    assert report.agent.fix_rounds == 0  # retrying cannot help
    assert not report.passed
    assert {c.status for c in report.verification.checks} == {"unavailable"}
    record = TaskRecord.load(run_copy.run_dir / "task.json")
    assert record.steps[-1]["status"] == "unavailable" and not record.steps[-1]["ok"]
    assert record.data["result"]["passed"] is False


def test_calls_after_a_failed_finish_in_the_same_reply_are_not_run(run_copy):
    ctl, _, _ = controller(run_copy.repo, [
        ModelReply(tool_calls=[ToolCall("finish", {"summary": "done"}),
                               ToolCall("edit_file", {"path": "src/app/model.py",
                                                      "old": "return qty <= 10",
                                                      "new": "return 0 < qty <= 10"})]),
    ])
    run = ctl.run()  # the script then runs out: model error
    assert run.actions == 0
    assert (run_copy.repo / "src/app/model.py").read_text() == ORIGINAL


def test_task_without_agent_checks_finishes_at_once(run_copy):
    task = replace(TASK, agent_checks=[])
    ctl, _, sandbox = controller(run_copy.repo, [tool_reply("finish", summary="done")], task=task)
    run = ctl.run()
    assert run.stop_reason == FINISHED and sandbox.ran == []


# ---- action limits per task -----------------------------------------------------

def test_feature_task_sets_its_own_action_limit_and_stage1_keeps_the_default():
    config = load_config()
    feature = load_task(PROJECT_ROOT / "tasks" / "batch_expiry.toml")
    stage1 = load_task(PROJECT_ROOT / "tasks" / "invalid_quantity.toml")
    assert effective_limits(feature, config.limits).max_actions == 40
    assert effective_limits(stage1, config.limits).max_actions == config.limits.max_actions == 30
    assert config.limits.max_actions_ceiling == 60 and config.limits.max_fix_rounds == 3


def test_task_limit_is_enforced(run_copy):
    task = replace(TASK, max_actions=4)
    ctl, _, _ = controller(run_copy.repo, [tool_reply("list_files")],
                           limits=effective_limits(task, LIMITS), repeat_last=True)
    run = ctl.run()
    assert run.stop_reason == ACTION_LIMIT and run.actions == 4


def test_task_limit_above_the_ceiling_is_refused():
    with pytest.raises(ValueError, match="above the ceiling of 60"):
        effective_limits(replace(TASK, max_actions=61), replace(LIMITS, max_actions_ceiling=60))


@pytest.mark.parametrize("value", ["0", "-3", '"40"', "true", "2.5"])
def test_invalid_task_limits_are_refused(tmp_path, value):
    path = tmp_path / "task.toml"
    path.write_text(f'project = "cosmicpython"\nmax_actions = {value}\ndescription = "x"\n'
                    'editable_files = ["a.py"]\nagent_checks = []\nfinal_checks = ["unit_tests"]\n')
    with pytest.raises(ValueError, match="max_actions must be a positive whole number"):
        load_task(path)


def test_cli_refuses_a_task_limit_above_the_ceiling_before_anything_runs(tmp_path, capsys,
                                                                       monkeypatch):
    path = tmp_path / "greedy.toml"
    path.write_text('project = "cosmicpython"\nmax_actions = 500\ndescription = "x"\n'
                    'editable_files = ["a.py"]\nagent_checks = []\nfinal_checks = ["unit_tests"]\n')
    monkeypatch.setattr("harness.cli.create_project_copy",
                        lambda *a, **k: pytest.fail("no run copy may be made"))
    assert main(["run", str(path)]) == 2
    assert "above the ceiling" in capsys.readouterr().err


def test_config_limit_above_its_own_ceiling_is_refused(tmp_path):
    text = (PROJECT_ROOT / "config" / "target.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(text.replace("max_actions = 30 ", "max_actions = 70 "))
    with pytest.raises(ValueError, match="max_actions must be between 1 and max_actions_ceiling"):
        load_config(path)
