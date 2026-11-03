"""The agent controller: ask the model for an action, check it, run it, repeat.

    read task -> ask model -> validate request -> run tool or return error
                    ^                                      |
                    +------------ send result back --------+

Edit-test-fix loop: when the model calls finish, the controller runs the
task's agent checks itself. If they all pass, the run is finished. If one
fails, its output goes back to the model with a note from the harness, and
the model continues: one fix round. After max_fix_rounds failed rounds the
run stops. If a check cannot run at all, the run stops at once.

Other stopping rules: too many actions, too many refused actions, too many
replies in a row without a usable tool call, a model error. Whatever the
reason, the harness verifies the result separately afterwards; the
controller never decides whether the task succeeded.

Every action and every finish attempt is a step. Each step is attached to its
tool result (for the context summary) and passed to on_step (for the task
record), with its phase: exploring, editing, checking, finish checks, or
fix round N for everything after the Nth failed finish.

For sessions, on_pending is called just before an action or the finish
checks run (so an interruption during it can be detected later), and
on_checkpoint after every completed step and when the run stops. The
controller itself never writes files.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from harness.actions import Action, ValidationError, normalize_path, tool_schemas, validate
from harness.config import ContextConfig, LimitsConfig, Task
from harness.context import ContextManager
from harness.model import ModelClient, ModelError, ModelReply
from harness.navigation import CodeNavigator
from harness.output import shorten
from harness.repo_tools import RepositoryTools, ToolError
from harness.sandbox import CheckResult, SandboxError

NO_TOOL_CALL_NUDGE = (
    "Your reply did not contain a tool call. Use one of the tools, "
    "or call finish if the task is complete."
)

# Why a run ended.
FINISHED = "finished"  # the model called finish and the agent checks passed
CHECKS_FAILING = "checks still failing after the last fix round"
CHECKS_UNAVAILABLE = "checks could not run"
ACTION_LIMIT = "action limit reached"
DENIED_LIMIT = "too many refused actions"
RETRY_LIMIT = "too many replies without a tool call"
MODEL_ERROR = "model error"
INTERNAL_ERROR = "internal error"


@dataclass(frozen=True)
class Event:
    """One line of progress, for the interface to show and for the run log."""
    kind: str  # thinking, model, action, result, refused, retry, finish, check, context, stop
    message: str
    elapsed_seconds: float = 0.0  # since the agent run started


@dataclass
class RunResult:
    stop_reason: str
    summary: str | None = None  # the model's own summary, if it called finish
    actions: int = 0  # tool calls that were attempted (finish not counted)
    denied: int = 0  # tool calls that were refused or failed
    retries: int = 0  # replies without a usable tool call
    fix_rounds: int = 0  # finish attempts whose agent checks failed
    steps: int = 0  # steps recorded: actions plus finish attempts
    stop_detail: str = ""  # e.g. the model error message
    checks: list[dict] = field(default_factory=list)  # every check run during the agent loop
    duration_seconds: float = 0.0  # from the first model request to the stop
    messages: list[dict] = field(default_factory=list)  # full history, never shortened
    events: list[Event] = field(default_factory=list)
    context: dict = field(default_factory=dict)  # size statistics from the ContextManager

    @property
    def finished(self) -> bool:
        """True if the model called finish and the agent checks then passed.

        Says nothing about the final checks (such as the hidden acceptance
        check), which the harness runs separately.
        """
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
        navigator: CodeNavigator | None = None,
        context: ContextConfig | None = None,
        on_step: Callable[[dict], None] | None = None,
        on_pending: Callable[[dict], None] | None = None,
        on_checkpoint: Callable[[RunResult, dict], None] | None = None,
    ):
        self.model = model
        self.tools = tools
        # Symbols, outlines, dependencies and Git state, on the same files.
        self.navigator = navigator or CodeNavigator(tools)
        self.context_config = context or ContextConfig()
        self.sandbox = sandbox
        self.task = task
        self.limits = limits
        self.on_event = on_event
        self.on_step = on_step
        self.on_pending = on_pending
        self.on_checkpoint = on_checkpoint
        self.schemas = tool_schemas(task.agent_checks)

    # ---- the loop ----------------------------------------------------------

    def run(self) -> RunResult:
        self._start = time.monotonic()
        self.context = ContextManager(self.task, self.navigator, self.context_config, self.schemas,
                                      fix_rounds=self.limits.max_fix_rounds)
        run = RunResult(stop_reason="", messages=self.context.initial_messages())
        retries_in_a_row = 0

        while True:
            # The model gets a prepared view; run.messages keeps the full history.
            view = self.context.view(run.messages)
            if self.context.last_compaction:
                self._emit(run, "context", self.context.last_compaction)
            try:
                reply = self.model.next_reply(view, self.schemas)
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
                self._checkpoint(run)
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
                    self._add_tool_result(run, str(call.name), f"Error: {exc}",
                                          self._record(run, None, ok=False, status="refused"))
                    if run.denied > self.limits.max_denied_actions:
                        return self._stop(run, DENIED_LIMIT)
                    self._checkpoint(run)
                    continue

                # 2. finish: the harness runs the agent checks. Not counted as an action.
                if action.name == "finish":
                    run.summary = action.arguments["summary"]
                    stop = self._finish_attempt(run)
                    if stop:
                        return stop
                    break  # not finished: ignore any later calls in this reply, ask again

                # 3. Enforce the action limit, then run the tool.
                if run.actions >= self.limits.max_actions:
                    return self._stop(run, ACTION_LIMIT)
                run.actions += 1
                self._emit(run, "action", _describe(action))
                self._pending(run, action.name, _target(action), run.actions)
                self._last_check = None
                try:
                    output = self._execute(action)
                except (ToolError, SandboxError) as exc:
                    run.denied += 1
                    self._emit(run, "refused", f"{action.name}: {exc}")
                    self._add_tool_result(run, action.name, f"Error: {exc}",
                                          self._record(run, action, ok=False, status="error"))
                    if run.denied > self.limits.max_denied_actions:
                        return self._stop(run, DENIED_LIMIT)
                    self._checkpoint(run)
                    continue
                except Exception as exc:  # a bug in the harness, not the model
                    return self._stop(run, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

                self._emit(run, "result", output.splitlines()[0] if output else "(no output)")
                check = self._last_check
                step = self._record(run, action, ok=True, status=check.status if check else None)
                if check:
                    self._keep_check(run, check, "model", step["step"])
                self._add_tool_result(run, action.name, output, step)
                self._checkpoint(run)

    # ---- the edit-test-fix loop ---------------------------------------------

    def _finish_attempt(self, run: RunResult) -> RunResult | None:
        """Run the agent checks after a finish call.

        Returns the final RunResult when the run ends here, or None when the
        model gets another fix round.
        """
        if not self.task.agent_checks:
            self._record(run, None, ok=True, status="no agent checks", tool="finish")
            return self._stop(run, FINISHED, run.summary)

        names = ", ".join(self.task.agent_checks)
        self._emit(run, "finish", f"The model called finish. The harness runs: {names}")
        self._pending(run, "finish checks", names, None)
        results = [self.sandbox.run_check(name) for name in self.task.agent_checks]
        for result in results:
            self._emit(run, "check", f"{result.name} {result.status.upper()}")
        passed = all(r.passed for r in results)
        unavailable = [r.name for r in results if r.status == "unavailable"]
        status = "passed" if passed else ("unavailable" if unavailable else "failed")
        summary = ", ".join(f"{r.name} {r.status}" for r in results)
        step = self._record(run, None, ok=passed, status=status, tool="finish",
                            target=summary)
        for result in results:
            self._keep_check(run, result, "finish", step["step"])

        if passed:
            return self._stop(run, FINISHED, run.summary)
        output = "\n\n".join(format_check(r) for r in results)
        self._add_tool_result(run, "finish", output, step)
        if unavailable:
            # Retrying cannot help when the checks cannot run at all.
            return self._stop(run, CHECKS_UNAVAILABLE, ", ".join(unavailable))
        run.fix_rounds += 1
        if run.fix_rounds > self.limits.max_fix_rounds:
            return self._stop(run, CHECKS_FAILING, summary)
        # Trusted instruction from the harness; the check output above is untrusted.
        run.messages.append({"role": "user", "content": (
            f"Not finished yet: after your finish call the harness ran the checks "
            f"({names}), and they did not all pass. The output is in the result above. "
            f"Fix the problems, then call finish again. This is fix round "
            f"{run.fix_rounds} of {self.limits.max_fix_rounds}."
        )})
        self._emit(run, "retry", f"Checks failed: fix round {run.fix_rounds} of "
                                 f"{self.limits.max_fix_rounds}")
        self._checkpoint(run)
        return None

    # ---- tools -------------------------------------------------------------

    def _execute(self, action: Action) -> str:
        args = action.arguments
        if action.name == "list_files":
            return self.tools.list_files(args.get("path", "."))
        if action.name == "read_file":
            return self.tools.read_file(args["path"])
        if action.name == "search":
            return self.tools.search(args["query"], args.get("path", "."))
        if action.name == "find_symbol":
            return self.navigator.find_symbol(args["name"])
        if action.name == "outline":
            return self.navigator.outline(args["path"])
        if action.name == "dependencies":
            return self.navigator.dependencies(args["path"])
        if action.name == "git_status":
            return self.navigator.git_status()
        if action.name == "git_diff":
            return self.navigator.git_diff(args.get("path", ""))
        if action.name == "edit_file":
            return self.tools.edit_file(args["path"], args["old"], args["new"])
        if action.name == "run_check":
            result = self.sandbox.run_check(args["name"])
            self._last_check = result  # for the step record and the session
            return format_check(result)
        raise RuntimeError(f"No handler for validated action {action.name!r}")

    # ---- helpers -----------------------------------------------------------

    def _record(self, run: RunResult, action: Action | None, ok: bool, status: str | None,
                tool: str | None = None, target: str | None = None) -> dict:
        """The harness's own record of one step: for the task record and the context summary.

        Only validated values are kept: paths, check names and identifiers. Free
        text such as search queries or edit contents is left out on purpose.
        """
        tool = tool or (action.name if action else "refused request")
        if action is not None and target is None:
            target = _target(action)
        if run.fix_rounds:
            phase = f"fix round {run.fix_rounds}"
        elif tool == "finish":
            phase = "finish checks"
        else:
            phase = {"edit_file": "editing", "run_check": "checking",
                     "refused request": "refused"}.get(tool, "exploring")
        run.steps += 1
        step = {"step": run.steps, "action": run.actions if action else None, "phase": phase,
                "tool": tool, "target": target, "ok": ok, "status": status,
                "elapsed_seconds": round(time.monotonic() - self._start, 1)}
        if self.on_step:
            self.on_step(step)
        return step

    def _pending(self, run: RunResult, tool: str, target: str | None, action: int | None) -> None:
        """Announce the step that is about to run, before it runs."""
        if self.on_pending:
            self.on_pending({"step": run.steps + 1, "action": action, "tool": tool,
                             "target": target})

    def _checkpoint(self, run: RunResult) -> None:
        if self.on_checkpoint:
            self.on_checkpoint(run, self.context.state())

    @staticmethod
    def _keep_check(run: RunResult, result: CheckResult, by: str, step: int) -> None:
        run.checks.append({
            "step": step, "by": by, "name": result.name, "status": result.status,
            "exit_code": result.exit_code, "duration_seconds": round(result.duration_seconds, 2),
            "error": result.error, "output_tail": result.output[-1500:],
        })

    def _add_tool_result(self, run: RunResult, name: str, content: str, step: dict) -> None:
        content = shorten(content, self.limits.max_tool_output_chars)
        run.messages.append({"role": "tool", "tool_name": _short(name), "content": content,
                             "harness_step": step})

    def _emit(self, run: RunResult, kind: str, message: str) -> None:
        event = Event(kind, message, round(time.monotonic() - self._start, 1))
        run.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _stop(self, run: RunResult, reason: str, detail: str = "") -> RunResult:
        run.stop_reason = reason
        run.stop_detail = detail
        run.context = self.context.stats()
        run.duration_seconds = round(time.monotonic() - self._start, 1)
        self._emit(run, "stop", f"{reason}: {detail}" if detail else reason)
        self._checkpoint(run)
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


# Tools whose validated argument may appear in the harness's step records.
PATH_TOOLS = {"list_files", "read_file", "outline", "dependencies", "git_diff", "edit_file"}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _target(action: Action) -> str | None:
    """The validated argument of an action that may appear in records."""
    args = action.arguments
    if action.name in PATH_TOOLS and "path" in args:
        return normalize_path(args["path"])[:200]
    if action.name == "run_check":
        return args["name"]  # validated against the task's checks
    if action.name == "find_symbol" and IDENTIFIER.fullmatch(args["name"].strip()):
        return args["name"].strip()
    return None
