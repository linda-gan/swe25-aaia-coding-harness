"""Command-line interface. Parses arguments and prints; the work is done in
the core modules (runner, projects, workspace).

    python -m harness projects                             allowed projects and their tasks
    python -m harness fetch cosmicpython                   make the project's pristine clone
    python -m harness run tasks/invalid_quantity.toml      agent run + verification
    python -m harness baseline tasks/invalid_quantity.toml checks on the starting code
    python -m harness sessions                             saved sessions and their status
    python -m harness show <session id>                    one session in detail
    python -m harness cancel <session id>                  stop a running session

run and baseline take --project NAME; it must be allowed and match the task.

Exit codes: 0 all checks passed, 1 checks failed, 2 setup problem,
130 interrupted with Ctrl+C.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from harness.config import PROJECT_ROOT, HarnessConfig, load_config, load_task
from harness.controller import Event, RunResult
from harness.ollama_client import OllamaClient
from harness.projects import TaskEntry, list_tasks, pristine_status, select_project
from harness.runner import RunReport, check_task, effective_limits, make_sandbox, run_task
from harness.session import CANCELLED, INTERRUPTED, Session, SessionError, SessionStore
from harness.verification import VerificationReport, verify
from harness.workspace import RUNS_DIR, WorkspaceError, create_project_copy, fetch_project

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

    def status(self, status: str, width: int = 0) -> str:
        """PASSED in green, anything else in red. Padded before coloring, because
        color codes would otherwise count as characters and break the columns."""
        label = status.upper().ljust(width)
        return self.green(label) if status == "passed" else self.red(label)


EVENT_COLORS = {"thinking": "dim", "refused": "red", "retry": "yellow", "stop": "bold",
                "finish": "cyan", "check": "cyan", "context": "yellow"}


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
        f"{agent.fix_rounds} fix rounds, {agent.duration_seconds:.1f}s)"
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
    width = max([12, *(len(check.name) for check in report.checks)])  # name column
    for check in report.checks:
        detail = check.error or last_line(check.output) or "(no output)"
        lines.append(f"  {check.name:<{width}} {style.status(check.status, 8)}  {detail}")
        if not check.passed and check.output.strip():
            # Failed, timed-out and unavailable checks show their output.
            lines.extend("      " + line for line in check.output.rstrip().splitlines())

    scope = report.scope
    if scope.passed:
        lines.append(f"  {'scope':<{width}} {style.status('passed', 8)}  only editable files changed")
    else:
        lines.append(f"  {'scope':<{width}} {style.status('failed', 8)}  "
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
            "Note: the model reported the task as done and its own checks passed, "
            "but the final checks did not pass."))
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


def shown_path(path: Path) -> str:
    """A path relative to the harness folder when it lies inside it."""
    path = Path(path).resolve()
    return str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)


def format_projects(style: Style, config: HarnessConfig, statuses: dict[str, str],
                    tasks: list[TaskEntry]) -> str:
    lines = [style.bold("Allowed projects (config/target.toml)")]
    for name, project in config.projects.items():
        status = statuses[name]
        lines.append(heading(style, name))
        lines.append(f"  Repository: {project.url}")
        lines.append(f"  Commit:     {project.commit}")
        lines.append(f"  Image:      {project.sandbox.image}")
        lines.append(f"  Clone:      {shown_path(project.pristine)} "
                     f"({style.green(status) if status == 'ready' else style.yellow(status)})")
        lines.append(f"  Checks:     {', '.join(project.checks)}")
        own = [t for t in tasks if t.task is not None and t.task.project == name]
        lines.append("  Tasks:      " + (", ".join(shown_path(t.path) for t in own) or "(none)"))

    other = [t for t in tasks if t.task is not None and t.task.project not in config.projects]
    if other:
        lines.append(heading(style, "Tasks for projects that are not allowed (cannot run)"))
        lines.extend(f"  {shown_path(t.path)}  (project {t.task.project!r})" for t in other)
    broken = [t for t in tasks if t.task is None]
    if broken:
        lines.append(heading(style, "Task files that could not be loaded"))
        lines.extend(f"  {shown_path(t.path)}: {t.error}" for t in broken)
    return "\n".join(lines)


STATUS_COLORS = {"passed": "green", "failed": "red", INTERRUPTED: "yellow",
                 CANCELLED: "yellow", "running": "cyan", "verifying": "cyan"}


def color_status(style: Style, status: str, width: int = 0) -> str:
    """Padded before coloring, so the columns stay aligned."""
    return getattr(style, STATUS_COLORS.get(status, "bold"))(status.ljust(width))


def format_sessions(style: Style, sessions: list[Session]) -> str:
    if not sessions:
        return "No sessions yet. Start one with: python -m harness run <task file>"
    lines = [style.bold(f"{'Session':<24} {'Status':<12} {'Steps':>5} {'Actions':>8} "
                        f"{'Fix':>4}  {'Updated':<19}  Task")]
    for session in sessions:
        data, record = session.data, session.task_record()
        limit = record.data["limits"]["max_actions"] if record else "?"
        counters = data["counters"]
        status = session.status
        lines.append(
            f"{session.session_id:<24} {color_status(style, status, 12)} "
            f"{counters['steps']:>5} {str(counters['actions']) + '/' + str(limit):>8} "
            f"{counters['fix_rounds']:>4}  {data['updated_at']:<19}  {shown_path(data['task_file'])}"
        )
        if session.uncertain_action:
            lines.append(style.yellow(f"{'':<24} uncertain: {describe_pending(session.uncertain_action)}"))
    return "\n".join(lines)


def describe_pending(pending: dict) -> str:
    target = f" {pending['target']}" if pending.get("target") else ""
    text = f"{pending['tool']}{target}"
    if pending.get("step") is not None:
        text = f"step {pending['step']} ({text})"
    if pending.get("started_at"):
        text += f", started {pending['started_at']}"
    return text


def format_session(style: Style, session: Session, history: bool = False) -> str:
    data, record = session.data, session.task_record()
    counters, status = data["counters"], session.status
    task = record.data if record else {}
    limits = task.get("limits", {})
    lines = [style.bold(f"Session {session.session_id}")]
    lines.append(f"  Status:   {color_status(style, status)}")
    if data.get("cancel_reason") and status == CANCELLED:
        lines.append(f"  Cancelled by: {data['cancel_reason']}")
    if session.uncertain_action:
        lines.append(style.yellow(
            f"  Uncertain action: {describe_pending(session.uncertain_action)}. The run stopped "
            "while it was in progress, so it is not known whether it completed."))
    lines.append(f"  Task:     {shown_path(data['task_file'])}")
    if task:
        lines.append(f"  Project:  {task['project']['name']} at starting commit "
                     f"{task['starting_commit'][:12]}")
    lines.append(f"  Model:    {data['model']}")
    lines.append(f"  Copy:     {data['repo']}")
    lines.append(f"  Updated:  {data['updated_at']} (created {data['created_at']})")
    lines.append(f"  Used:     {counters['actions']} of {limits.get('max_actions', '?')} actions, "
                 f"{counters['denied']} refused, {counters['retries']} retries, "
                 f"{counters['fix_rounds']} of {limits.get('max_fix_rounds', '?')} fix rounds")
    stopped = data["stop_reason"] or "(not stopped)"
    if data.get("stop_detail") and data["stop_reason"] != "finished":
        stopped += f": {data['stop_detail']}"
    lines.append(f"  Stopped:  {stopped}")

    if task:
        lines.append(heading(style, "Request"))
        lines.extend("  " + line for line in task["request"].splitlines())
        lines.append(heading(style, "Allowed changes"))
        lines.extend(f"  {path}" for path in task["allowed_changes"])

    progress = session.progress()
    lines.append(heading(style, "Progress (steps per phase)"))
    lines.append("  " + (", ".join(f"{phase} {n}" for phase, n in progress.items()) or "(no steps yet)"))

    lines.append(heading(style, "Steps"))
    for step in (record.steps if record else []):
        target = f" {step['target']}" if step.get("target") else ""
        outcome = step.get("status") or ("ok" if step["ok"] else "error")
        line = f"  {step['step']:>3}  {step['phase']:<14} {step['tool']}{target}  [{outcome}]"
        lines.append(line if step["ok"] else style.red(line))
    # A step that started but did not complete: running now, or uncertain.
    open_step = session.uncertain_action or (data.get("pending_action")
                                             if status in ("running", "verifying") else None)
    if open_step:
        label = "running now" if status in ("running", "verifying") else "UNCERTAIN"
        number = open_step["step"] if open_step.get("step") is not None else "-"
        lines.append(style.yellow(f"  {number:>3}  {label:<14} {open_step['tool']}"
                                  f"{' ' + open_step['target'] if open_step.get('target') else ''}"))

    lines.append(heading(style, "Checks during the run"))
    if data["checks"]:
        for check in data["checks"]:
            lines.append(f"  step {check['step']:>3}  by {check['by']:<6} {check['name']:<20} "
                         f"{style.status(check['status'])}")
    else:
        lines.append("  (none)")

    lines.append(heading(style, "Errors"))
    if data["errors"]:
        for error in data["errors"]:
            where = f"step {error['step']}" if error["step"] else "run"
            first = error["message"].splitlines()[0]
            lines.append(style.red(f"  {where}: {first}"))
    else:
        lines.append("  (none)")

    lines.append(heading(style, "Working copy (changed files)"))
    files = data["working_copy"]["files"]
    if files:
        lines.extend(f"  {path}  {(digest or 'deleted')[:12]}" for path, digest in files.items())
    else:
        lines.append("  (no changes)")

    lines.append(heading(style, "Final verification"))
    verification = data.get("verification")
    if verification:
        width = max([12, *(len(c["name"]) for c in verification["checks"])])
        for check in verification["checks"]:
            detail = check["error"] or last_line(check["output_tail"]) or ""
            lines.append(f"  {check['name']:<{width}} {style.status(check['status'], 8)}  {detail}")
        scope = verification["scope"]
        lines.append(f"  {'scope':<{width}} {style.status('passed' if scope['passed'] else 'failed', 8)}"
                     + ("" if scope["passed"] else f"  changed outside the task: {', '.join(scope['outside'])}"))
        lines.append(style.bold(style.green("  RESULT: PASSED") if verification["passed"]
                                else style.red("  RESULT: FAILED")))
    else:
        lines.append("  not run" + {INTERRUPTED: " (the session was interrupted)",
                                     CANCELLED: " (the session was cancelled)"}.get(status, " yet"))

    lines.append(heading(style, "History"))
    messages = data["messages"]
    lines.append(f"  {len(messages)} messages saved"
                 + ("" if history else "; show them with --history"))
    if history:
        for i, message in enumerate(messages):
            who = message["role"] + (f" {message['tool_name']}" if message.get("tool_name") else "")
            text = " ".join(str(message.get("content", "")).split())
            if message.get("tool_calls"):
                text += " -> " + ", ".join(c["function"]["name"] for c in message["tool_calls"])
            lines.append(f"  {i:>3} {who:<16} {text[:150]}")
    return "\n".join(lines)


# ---- commands ----------------------------------------------------------------

def prepare(args):
    """Load the config and the task, and select the project: refuse early if anything is off."""
    config = load_config()
    task = load_task(args.task)
    project = select_project(config, task, args.project)
    check_task(task, project)
    effective_limits(task, config.limits)  # refuses a task limit above the ceiling
    return config, task, project


def cmd_sessions(args, style: Style) -> int:
    print(format_sessions(style, SessionStore(args.runs_dir).all()))
    return EXIT_PASSED


def cmd_show(args, style: Style) -> int:
    session = SessionStore(args.runs_dir).open(args.session)
    print(format_session(style, session, history=args.history))
    return EXIT_PASSED


def cmd_cancel(args, style: Style) -> int:
    store = SessionStore(args.runs_dir)
    session = store.open(args.session)
    pid = session.request_cancel()
    print(f"Cancel requested for session {session.session_id} (process {pid}).")
    print(f"Waiting up to {args.wait:g}s for it to stop at a safe point...")
    if not session.wait_until_stopped(args.wait):
        print(style.yellow("It has not stopped yet. Check again with: python -m harness sessions"))
        return EXIT_FAILED
    session = Session.load(session.run_dir)
    print(f"Stopped. Status: {color_status(style, session.status)}")
    if session.uncertain_action:
        print(style.yellow(f"Uncertain action: {describe_pending(session.uncertain_action)}"))
    print(f"Details: python -m harness show {session.session_id}")
    return EXIT_PASSED


def cmd_projects(args, style: Style) -> int:
    config = load_config()
    statuses = {name: pristine_status(project) for name, project in config.projects.items()}
    print(format_projects(style, config, statuses, list_tasks()))
    return EXIT_PASSED


def cmd_fetch(args, style: Style) -> int:
    project = load_config().project(args.project)  # only allowed projects can be fetched
    print(f"Fetching {project.name}: {project.url} @ {project.commit[:12]}")
    if fetch_project(project, force=args.force):
        print(style.green(f"Pristine clone ready at {shown_path(project.pristine)}."))
    else:
        print(f"The pristine clone at {shown_path(project.pristine)} is already at the pinned "
              "commit and unchanged; nothing to do (--force fetches it again).")
    return EXIT_PASSED


def cmd_run(args, style: Style) -> int:
    config, task, project = prepare(args)
    model_config = config.model
    if args.model:
        model_config = dataclasses.replace(model_config, name=args.model)
    if args.no_think:
        model_config = dataclasses.replace(model_config, think=False)

    run_copy = create_project_copy(project)
    print(style.bold("Coding harness"))
    print(f"  Task:    {args.task}")
    print(f"  Project: {project.name} ({project.url} @ {project.commit[:12]})")
    print(f"  Copy:    {run_copy.repo}")
    limits = effective_limits(task, config.limits)
    print(f"  Limits:  {limits.max_actions} actions, {limits.max_fix_rounds} fix rounds")
    print(f"  Session: {run_copy.run_id}  (python -m harness show {run_copy.run_id})")
    print(f"  Model:   {model_config.name} (thinking {'on' if model_config.think else 'off'})")
    print(heading(style, "Task"))
    print(task.description)
    print(heading(style, "Agent"))

    try:
        report = run_with_progress(args, style, config, task, project, model_config, run_copy)
    except KeyboardInterrupt:
        print(style.yellow(f"\nThe session is saved and shows as interrupted: "
                           f"python -m harness show {run_copy.run_id}"), file=sys.stderr)
        raise
    print()
    print(format_agent(style, report.agent))
    if report.cancelled:
        print(style.yellow(f"Cancelled ({report.agent.stop_detail}). No further actions ran, "
                           "and no verification was run."))
        if report.agent.interrupted_action:
            print(style.yellow("Uncertain action: "
                               f"{describe_pending(report.agent.interrupted_action)}"))
        print(f"Session: python -m harness show {run_copy.run_id}")
        return EXIT_INTERRUPTED
    print(format_verification(style, report.verification))
    print()
    print(format_result(style, report))
    print(f"Session: python -m harness show {run_copy.run_id}")
    return EXIT_PASSED if report.passed else EXIT_FAILED


def run_with_progress(args, style, config, task, project, model_config, run_copy) -> RunReport:
    return run_task(
        task=task,
        task_path=args.task,
        config=config,
        project=project,
        model=OllamaClient(model_config),
        model_name=model_config.name,
        run_copy=run_copy,
        on_event=lambda e: print(format_event(style, e), flush=True),
    )


def cmd_baseline(args, style: Style) -> int:
    """Run the final checks on the unchanged code, to show the starting state."""
    config, task, project = prepare(args)
    run_copy = create_project_copy(project)
    print(style.bold("Baseline: final checks on the unchanged starting code"))
    print(f"  Project: {project.name} ({project.url} @ {project.commit[:12]})")
    print(f"  Copy:    {run_copy.repo}")
    report = verify(make_sandbox(run_copy, project, task), run_copy.repo, task)
    print(format_verification(style, report))
    print()
    print("This is the starting state. For a new task, bug fix or feature, the "
          "acceptance check is expected to fail here.")
    return EXIT_PASSED if report.passed else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness",
        description="Turn a coding task into a verified code change you can review.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("projects", help="list the allowed projects, their clones and tasks")

    fetch = sub.add_parser("fetch", help="make an allowed project's pristine clone")
    fetch.add_argument("project", help="project name from config/target.toml, e.g. cosmicpython")
    fetch.add_argument("--force", action="store_true", help="fetch again even if the clone is fine")

    project_help = "project to work on; must be allowed and match the task (default: the task's)"
    run = sub.add_parser("run", help="run the agent on a task, then verify the result")
    run.add_argument("task", type=Path, help="task file, e.g. tasks/invalid_quantity.toml")
    run.add_argument("--project", help=project_help)
    run.add_argument("--model", help="Ollama model name (default: from config)")
    run.add_argument("--no-think", action="store_true", help="turn thinking mode off")

    baseline = sub.add_parser("baseline", help="run the task's final checks on the starting code")
    baseline.add_argument("task", type=Path, help="task file, e.g. tasks/invalid_quantity.toml")
    baseline.add_argument("--project", help=project_help)

    runs_help = "folder with the run folders (default: workspace/runs)"
    sessions = sub.add_parser("sessions", help="list saved sessions and their status")
    sessions.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help=runs_help)

    show = sub.add_parser("show", help="show one session: task, progress, steps, checks, errors")
    show.add_argument("session", help="session ID, or a unique start or end of it")
    show.add_argument("--history", action="store_true", help="also list the saved conversation")
    show.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help=runs_help)

    cancel = sub.add_parser("cancel", help="stop a running session at its next safe point")
    cancel.add_argument("session", help="session ID, or a unique start or end of it")
    cancel.add_argument("--wait", type=float, default=60, help="seconds to wait for it to stop")
    cancel.add_argument("--runs-dir", type=Path, default=RUNS_DIR, help=runs_help)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    style = Style()
    command = {
        "projects": cmd_projects, "fetch": cmd_fetch, "run": cmd_run, "baseline": cmd_baseline,
        "sessions": cmd_sessions, "show": cmd_show, "cancel": cmd_cancel,
    }[args.command]
    try:
        return command(args, style)
    except (WorkspaceError, SessionError, ValueError, FileNotFoundError) as exc:
        print(style.red(f"Setup problem: {exc}"), file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        # Any running container is removed by the sandbox's cleanup on the way out.
        print(style.yellow("\nInterrupted. No verification was run for this attempt."),
              file=sys.stderr)
        return EXIT_INTERRUPTED
