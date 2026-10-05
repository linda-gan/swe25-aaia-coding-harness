"""The agent controller: ask the model for an action, check it, run it, repeat.

    read task -> ask model -> validate request -> run tool or return error
                    ^                                      |
                    +------------ send result back --------+

The loop stops when the model calls finish, or when a limit is reached:
too many actions, too many refused actions, or too many replies in a row
without a usable tool call. The controller never decides whether the task
succeeded; the harness checks that separately afterwards.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from harness.actions import Action, ValidationError, tool_schemas, validate
from harness.config import LimitsConfig, Task
from harness.model import ModelClient, ModelError, ModelReply
from harness.output import shorten
from harness.prompts import system_prompt
from harness.repo_tools import RepositoryTools, ToolError
from harness.sandbox import CheckResult, SandboxError

NO_TOOL_CALL_NUDGE = (
    "Your reply did not contain a tool call. Use one of the tools, "
    "or call finish if the task is complete."
)

# Why a run ended.
FINISHED = "finished"
ACTION_LIMIT = "action limit reached"
DENIED_LIMIT = "too many refused actions"
RETRY_LIMIT = "too many replies without a tool call"
MODEL_ERROR = "model error"
INTERNAL_ERROR = "internal error"


@dataclass(frozen=True)
class Event:
    """One line of progress, for the interface to show and for the run log."""
    kind: str  # thinking, model, action, result, refused, retry, stop
    message: str
    elapsed_seconds: float = 0.0  # since the agent run started


@dataclass
class RunResult:
    stop_reason: str
    summary: str | None = None  # the model's own summary, if it called finish
    actions: int = 0  # tool calls that were attempted (finish not counted)
    denied: int = 0  # tool calls that were refused or failed
    retries: int = 0  # replies without a usable tool call
    duration_seconds: float = 0.0  # from the first model request to the stop
    messages: list[dict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        """True if the model said it was done. Says nothing about whether checks pass."""
        return self.stop_reason == FINISHED


class AgentController:
    def __init__(
        self,
        model: ModelClient,
        tools: RepositoryTools,
        sandbox,  # anything with run_check(name) -> CheckResult
        task: Task,
        limits: LimitsConfig,
        on_event: Callable[[Event], None] | None = None,
    ):
        self.model = model
        self.tools = tools
        self.sandbox = sandbox
        self.task = task
        self.limits = limits
        self.on_event = on_event
        self.schemas = tool_schemas(task.agent_checks)

    # ---- the loop ----------------------------------------------------------

    def run(self) -> RunResult:
        self._start = time.monotonic()
        run = RunResult(stop_reason="", messages=[
            {"role": "system", "content": system_prompt(self.task)},
            {"role": "user", "content": self.task.description},
        ])
        retries_in_a_row = 0

        while True:
            try:
                reply = self.model.next_reply(run.messages, self.schemas)
            except ModelError as exc:
                return self._stop(run, MODEL_ERROR, f"The model request failed: {exc}")

            run.messages.append(_assistant_message(reply))
            if reply.thinking.strip():
                self._emit(run, "thinking", _short(" ".join(reply.thinking.split()), 300))
            if reply.text.strip():
                self._emit(run, "model", reply.text.strip())

            if not reply.tool_calls:
                run.retries += 1
                retries_in_a_row += 1
                self._emit(run, "retry", "Reply had no tool call.")
                if retries_in_a_row > self.limits.max_retries:
                    return self._stop(run, RETRY_LIMIT)
                run.messages.append({"role": "user", "content": NO_TOOL_CALL_NUDGE})
                continue
            retries_in_a_row = 0

            for call in reply.tool_calls:
                # 1. Check the request before anything runs.
                try:
                    action = validate(call, self.task.editable_files, self.task.agent_checks)
                except ValidationError as exc:
                    if run.actions >= self.limits.max_actions:
                        return self._stop(run, ACTION_LIMIT)
                    run.actions += 1
                    run.denied += 1
                    self._emit(run, "refused", f"{_short(call.name)}: {exc}")
                    self._add_tool_result(run, str(call.name), f"Error: {exc}")
                    if run.denied > self.limits.max_denied_actions:
                        return self._stop(run, DENIED_LIMIT)
                    continue

                # 2. finish ends the loop. It is not counted as an action.
                if action.name == "finish":
                    run.summary = action.arguments["summary"]
                    return self._stop(run, FINISHED, run.summary)

                # 3. Enforce the action limit, then run the tool.
                if run.actions >= self.limits.max_actions:
                    return self._stop(run, ACTION_LIMIT)
                run.actions += 1
                self._emit(run, "action", _describe(action))
                try:
                    output = self._execute(action)
                except (ToolError, SandboxError) as exc:
                    run.denied += 1
                    self._emit(run, "refused", f"{action.name}: {exc}")
                    self._add_tool_result(run, action.name, f"Error: {exc}")
                    if run.denied > self.limits.max_denied_actions:
                        return self._stop(run, DENIED_LIMIT)
                    continue
                except Exception as exc:  # a bug in the harness, not the model
                    return self._stop(run, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

                self._emit(run, "result", output.splitlines()[0] if output else "(no output)")
                self._add_tool_result(run, action.name, output)

    # ---- tools -------------------------------------------------------------

    def _execute(self, action: Action) -> str:
        args = action.arguments
        if action.name == "list_files":
            return self.tools.list_files(args.get("path", "."))
        if action.name == "read_file":
            return self.tools.read_file(args["path"])
        if action.name == "search":
            return self.tools.search(args["query"], args.get("path", "."))
        if action.name == "edit_file":
            return self.tools.edit_file(args["path"], args["old"], args["new"])
        if action.name == "run_check":
            return format_check(self.sandbox.run_check(args["name"]))
        raise RuntimeError(f"No handler for validated action {action.name!r}")

    # ---- helpers -----------------------------------------------------------

    def _add_tool_result(self, run: RunResult, name: str, content: str) -> None:
        content = shorten(content, self.limits.max_tool_output_chars)
        run.messages.append({"role": "tool", "tool_name": _short(name), "content": content})

    def _emit(self, run: RunResult, kind: str, message: str) -> None:
        event = Event(kind, message, round(time.monotonic() - self._start, 1))
        run.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _stop(self, run: RunResult, reason: str, detail: str = "") -> RunResult:
        run.stop_reason = reason
        run.duration_seconds = round(time.monotonic() - self._start, 1)
        self._emit(run, "stop", f"{reason}: {detail}" if detail else reason)
        return run


def format_check(result: CheckResult) -> str:
    header = f"Check {result.name}: {result.status.upper()} (exit code {result.exit_code})"
    if result.error:
        header += f"\n{result.error}"
    return f"{header}\n{result.output}".rstrip()


def _assistant_message(reply: ModelReply) -> dict:
    message: dict = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        message["tool_calls"] = [
            {"function": {"name": _short(c.name),
                          "arguments": c.arguments if isinstance(c.arguments, dict) else {}}}
            for c in reply.tool_calls
        ]
    return message


def _describe(action: Action) -> str:
    args = action.arguments
    if action.name == "edit_file":
        return f"edit_file {args['path']}"
    shown = ", ".join(f"{k}={_short(v, 60)!r}" for k, v in args.items())
    return f"{action.name}({shown})"


def _short(value, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."
