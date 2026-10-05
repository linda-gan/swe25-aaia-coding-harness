"""Controller tests with a scripted model: no real model, account or Docker needed.

Covers the handout tests "Controller", "Invalid request", "Action limit"
and the controller's part of "Failed command" and "Output limit".
"""
import pytest

from harness.config import LimitsConfig, Task
from harness.controller import (
    ACTION_LIMIT, DENIED_LIMIT, FINISHED, MODEL_ERROR, RETRY_LIMIT, AgentController,
)
from harness.model import ModelReply, ScriptedModelClient, ToolCall, tool_reply
from harness.repo_tools import RepositoryTools
from harness.sandbox import CheckResult

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"


class FakeSandbox:
    """Returns prepared check results and records which checks were run."""

    def __init__(self, exit_code=0, output="3 passed"):
        self.exit_code = exit_code
        self.output = output
        self.ran = []

    def run_check(self, name):
        self.ran.append(name)
        return CheckResult(
            name=name, command=["pytest"], exit_code=self.exit_code, output=self.output,
            timed_out=False, duration_seconds=0.1, container="fake",
        )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "model.py").write_text(ORIGINAL)
    (root / "tests").mkdir()
    (root / "tests" / "test_model.py").write_text("def test_x(): pass\n")
    return root


TASK = Task(
    description="Reject quantities of zero or less.",
    editable_files=["src/app/model.py"],
    agent_checks=["unit_tests"],
)


def make_controller(repo, replies, sandbox=None, repeat_last=False, **limits):
    values = dict(max_actions=10, max_denied_actions=3, max_retries=2, max_tool_output_chars=500)
    values.update(limits)
    model = ScriptedModelClient(replies, repeat_last=repeat_last)
    controller = AgentController(
        model=model,
        tools=RepositoryTools(repo),
        sandbox=sandbox or FakeSandbox(),
        task=TASK,
        limits=LimitsConfig(**values),
    )
    return controller, model


def tool_messages(run):
    return [m for m in run.messages if m["role"] == "tool"]


# ---- the normal path -------------------------------------------------------

def test_scripted_reply_calls_tool_and_model_receives_result(repo):
    controller, model = make_controller(repo, [
        tool_reply("read_file", path="src/app/model.py"),
        tool_reply("finish", summary="done"),
    ])
    run = controller.run()

    assert run.stop_reason == FINISHED
    assert run.actions == 1
    # The second model call was sent the file content as a tool result.
    last_sent = model.received[1][-1]
    assert last_sent["role"] == "tool"
    assert last_sent["tool_name"] == "read_file"
    assert "return qty <= 10" in last_sent["content"]


def test_full_fix_edits_file_and_runs_check(repo):
    sandbox = FakeSandbox()
    controller, _ = make_controller(repo, [
        tool_reply("read_file", path="src/app/model.py"),
        tool_reply("edit_file", path="src/app/model.py",
                   old="return qty <= 10", new="return 0 < qty <= 10"),
        tool_reply("run_check", name="unit_tests"),
        tool_reply("finish", summary="Quantities must be positive."),
    ], sandbox=sandbox)
    run = controller.run()

    assert run.stop_reason == FINISHED
    assert run.summary == "Quantities must be positive."
    assert "0 < qty <= 10" in (repo / "src/app/model.py").read_text()
    assert sandbox.ran == ["unit_tests"]
    assert [e.kind for e in run.events].count("action") == 3


def test_several_tool_calls_in_one_reply_all_run(repo):
    controller, _ = make_controller(repo, [
        ModelReply(tool_calls=[ToolCall("list_files", {}), ToolCall("read_file", {"path": "src/app/model.py"})]),
        tool_reply("finish", summary="done"),
    ])
    run = controller.run()
    assert [m["tool_name"] for m in tool_messages(run)] == ["list_files", "read_file"]


# ---- invalid requests ------------------------------------------------------

@pytest.mark.parametrize("bad_call", [
    ToolCall("delete_everything", {}),
    ToolCall("edit_file", {"path": "src/app/model.py"}),  # missing old and new
    ToolCall("edit_file", {"path": "tests/test_model.py", "old": "pass", "new": "assert 1"}),
    ToolCall("edit_file", {"path": "../outside.py", "old": "a", "new": "b"}),
    ToolCall("run_check", {"name": "acceptance"}),
])
def test_invalid_request_gives_error_and_runs_nothing(repo, bad_call):
    sandbox = FakeSandbox()
    controller, model = make_controller(repo, [
        ModelReply(tool_calls=[bad_call]),
        tool_reply("finish", summary="gave up"),
    ], sandbox=sandbox)
    run = controller.run()

    assert run.denied == 1
    assert tool_messages(run)[0]["content"].startswith("Error:")
    assert sandbox.ran == []
    assert (repo / "src/app/model.py").read_text() == ORIGINAL
    assert (repo / "tests/test_model.py").read_text() == "def test_x(): pass\n"
    # The model was told about the error and could continue.
    assert model.received[1][-1]["content"].startswith("Error:")


def test_too_many_refused_actions_stop_the_run(repo):
    controller, _ = make_controller(
        repo, [tool_reply("read_file", path="../secret.txt")], repeat_last=True,
        max_denied_actions=3,
    )
    run = controller.run()
    assert run.stop_reason == DENIED_LIMIT
    assert run.denied == 4  # the limit is 3, the 4th refusal stops the run


# ---- limits ----------------------------------------------------------------

def test_repeating_model_reaches_action_limit_and_stops(repo):
    controller, model = make_controller(
        repo, [tool_reply("list_files")], repeat_last=True, max_actions=5,
    )
    run = controller.run()

    assert run.stop_reason == ACTION_LIMIT
    assert run.actions == 5
    assert len(tool_messages(run)) == 5  # no 6th action ran
    assert len(model.received) == 6  # the 6th reply was received but not executed


def test_replies_without_tool_calls_are_retried_then_stopped(repo):
    controller, _ = make_controller(
        repo, [ModelReply(text="I think the bug is in model.py.")], repeat_last=True,
        max_retries=2,
    )
    run = controller.run()
    assert run.stop_reason == RETRY_LIMIT
    assert run.retries == 3
    assert run.actions == 0


def test_large_tool_output_is_shortened_before_reaching_the_model(repo):
    (repo / "src/app/big.py").write_text("x = 1\n" * 2000)
    controller, model = make_controller(repo, [
        tool_reply("read_file", path="src/app/big.py"),
        tool_reply("finish", summary="done"),
    ], max_tool_output_chars=500)
    controller.run()

    sent = model.received[1][-1]["content"]
    assert "[output shortened:" in sent
    assert len(sent) < 600


# ---- failures stay visible -------------------------------------------------

def test_failed_check_output_reaches_the_model(repo):
    sandbox = FakeSandbox(exit_code=1, output="FAILED test_model.py::test_x\n1 failed")
    controller, model = make_controller(repo, [
        tool_reply("run_check", name="unit_tests"),
        tool_reply("finish", summary="done"),
    ], sandbox=sandbox)
    run = controller.run()

    result = model.received[1][-1]["content"]
    assert "FAILED (exit code 1)" in result
    assert "1 failed" in result
    # finished only means the model said so; it is not a claim that checks pass.
    assert run.finished


def test_model_error_stops_the_run_cleanly(repo):
    controller, _ = make_controller(repo, [tool_reply("list_files")])  # script runs out
    run = controller.run()
    assert run.stop_reason == MODEL_ERROR
    assert run.events[-1].kind == "stop"


def test_thinking_is_shown_as_progress(repo):
    controller, _ = make_controller(repo, [
        ModelReply(thinking="The bug is probably in model.py.", tool_calls=[ToolCall("list_files", {})]),
        tool_reply("finish", summary="done"),
    ])
    run = controller.run()
    assert run.events[0].kind == "thinking"
    assert "probably in model.py" in run.events[0].message
