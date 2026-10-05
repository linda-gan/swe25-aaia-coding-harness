"""Command-line interface.

    python -m harness run tasks/invalid_quantity.toml      agent run + verification
    python -m harness baseline tasks/invalid_quantity.toml checks on the starting code

Exit codes: 0 all checks passed, 1 checks failed, 2 setup problem,
130 interrupted with Ctrl+C.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from harness.config import load_config, load_task
from harness.controller import Event, RunResult
from harness.ollama_client import OllamaClient
from harness.runner import RunReport, check_task, make_sandbox, run_task
from harness.verification import VerificationReport, verify
from harness.workspace import WorkspaceError, create_run_copy

EXIT_PASSED, EXIT_FAILED, EXIT_SETUP, EXIT_INTERRUPTED = 0, 1, 2, 130


# ---- colors ------------------------------------------------------------------

class Style:
    """ANSI colors, only when writing to a terminal and NO_COLOR is not set."""

    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def red(self, t): return self._wrap("31", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def cyan(self, t): return self._wrap("36", t)

    def status(self, status: str) -> str:
        label = status.upper()
        return self.green(label) if status == "passed" else self.red(label)


EVENT_COLORS = {"thinking": "dim", "refused": "red", "retry": "yellow", "stop": "bold"}


# ---- formatting --------------------------------------------------------------

def heading(style: Style, title: str) -> str:
    return "\n" + style.bold(f"== {title} ")


def format_event(style: Style, event: Event) -> str:
    line = f"[{event.elapsed_seconds:6.1f}s] {event.kind:<8} {event.message}"
    color = EVENT_COLORS.get(event.kind)
    return getattr(style, color)(line) if color else line


def format_agent(style: Style, agent: RunResult) -> str:
    lines = [
        f"Agent stopped: {style.bold(agent.stop_reason)} "
        f"({agent.actions} actions, {agent.denied} refused, {agent.retries} retries, "
        f"{agent.duration_seconds:.1f}s)"
    ]
    if agent.summary:
        lines.append(f"Model's summary: {agent.summary}")
    return "\n".join(lines)


def last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def format_diff(style: Style, diff_text: str) -> str:
    out = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            out.append(style.bold(line))
        elif line.startswith("+"):
            out.append(style.green(line))
        elif line.startswith("-"):
            out.append(style.red(line))
        elif line.startswith("@@"):
            out.append(style.cyan(line))
        else:
            out.append(line)
    return "\n".join(out)


def format_verification(style: Style, report: VerificationReport) -> str:
    lines = [heading(style, "Verification (run by the harness, not the model)")]
    for check in report.checks:
        detail = check.error or last_line(check.output) or "(no output)"
        lines.append(f"  {check.name:<12} {style.status(check.status):<8}  {detail}")
        if not check.passed and check.output.strip():
            # Failed, timed-out and unavailable checks show their output.
            lines.extend("      " + line for line in check.output.rstrip().splitlines())

    scope = report.scope
    if scope.passed:
        lines.append(f"  {'scope':<12} {style.status('passed'):<8}  only editable files changed")
    else:
        lines.append(f"  {'scope':<12} {style.status('failed'):<8}  "
                     f"changed outside the task: {', '.join(scope.outside)}")

    lines.append(heading(style, "Changed files"))
    if report.changed_files:
        lines.extend(f"  {status:<2} {path}" for status, path in report.changed_files)
    else:
        lines.append("  (none)")

    lines.append(heading(style, "Diff"))
    lines.append(format_diff(style, report.diff) if report.diff.strip() else "  (no changes)")
    return "\n".join(lines)


def format_result(style: Style, report: RunReport) -> str:
    lines = []
    if report.agent.finished and not report.passed:
        lines.append(style.yellow(
            "Note: the model reported the task as done, but the checks did not pass."))
    if not report.agent.finished:
        lines.append(style.yellow(
            f"Note: the agent did not finish ({report.agent.stop_reason}). "
            "The checks above show the state of the code anyway."))
    if report.passed:
        lines.append(style.bold(style.green("RESULT: PASSED - all checks passed.")))
    else:
        lines.append(style.bold(style.red("RESULT: FAILED - see the checks above.")))
    lines.append(f"Time: agent {report.agent.duration_seconds:.1f}s, verification "
                 f"{report.verification_seconds:.1f}s, total {report.total_seconds:.1f}s")
    lines.append(f"Run record: {report.run_copy.run_dir / 'run.json'}")
    return "\n".join(lines)


# ---- commands ----------------------------------------------------------------

def cmd_run(args, style: Style) -> int:
    config = load_config()
    task = load_task(args.task)
    check_task(task, config)
    model_config = config.model
    if args.model:
        model_config = dataclasses.replace(model_config, name=args.model)
    if args.no_think:
        model_config = dataclasses.replace(model_config, think=False)

    run_copy = create_run_copy(config.target.commit)
    print(style.bold("Coding harness"))
    print(f"  Task:   {args.task}")
    print(f"  Target: {config.target.url} @ {config.target.commit[:12]}")
    print(f"  Copy:   {run_copy.repo}")
    print(f"  Model:  {model_config.name} (thinking {'on' if model_config.think else 'off'})")
    print(heading(style, "Task"))
    print(task.description)
    print(heading(style, "Agent"))

    report = run_task(
        task=task,
        task_path=args.task,
        config=config,
        model=OllamaClient(model_config),
        model_name=model_config.name,
        run_copy=run_copy,
        on_event=lambda e: print(format_event(style, e), flush=True),
    )
    print()
    print(format_agent(style, report.agent))
    print(format_verification(style, report.verification))
    print()
    print(format_result(style, report))
    return EXIT_PASSED if report.passed else EXIT_FAILED


def cmd_baseline(args, style: Style) -> int:
    """Run the final checks on the unchanged code, to show the starting state."""
    config = load_config()
    task = load_task(args.task)
    check_task(task, config)
    run_copy = create_run_copy(config.target.commit)
    print(style.bold("Baseline: final checks on the unchanged starting code"))
    print(f"  Target: {config.target.url} @ {config.target.commit[:12]}")
    print(f"  Copy:   {run_copy.repo}")
    report = verify(make_sandbox(run_copy, config), run_copy.repo, task)
    print(format_verification(style, report))
    print()
    print("This is the starting state. For a bug-fix task the acceptance check "
          "is expected to fail here.")
    return EXIT_PASSED if report.passed else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness",
        description="Turn a coding task into a verified code change you can review.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent on a task, then verify the result")
    run.add_argument("task", type=Path, help="task file, e.g. tasks/invalid_quantity.toml")
    run.add_argument("--model", help="Ollama model name (default: from config)")
    run.add_argument("--no-think", action="store_true", help="turn thinking mode off")

    baseline = sub.add_parser("baseline", help="run the task's final checks on the starting code")
    baseline.add_argument("task", type=Path, help="task file, e.g. tasks/invalid_quantity.toml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    style = Style()
    command = {"run": cmd_run, "baseline": cmd_baseline}[args.command]
    try:
        return command(args, style)
    except (WorkspaceError, ValueError, FileNotFoundError) as exc:
        print(style.red(f"Setup problem: {exc}"), file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        # Any running container is removed by the sandbox's cleanup on the way out.
        print(style.yellow("\nInterrupted. No verification was run for this attempt."),
              file=sys.stderr)
        return EXIT_INTERRUPTED
