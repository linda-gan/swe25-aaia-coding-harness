"""Safe resume: decide whether a saved session may continue, and prepare it.

Before a session continues, everything it relies on is checked again:

  session       cancelled, interrupted or paused, and not in use
  project       still allowed, with the same URL and pinned commit
  task          the task file still exists, and its request, allowed changes,
                checks (with their exact commands) and acceptance folder are
                the same as when the run started
  limits        the same limits as when the run started, not used up
  working copy  the same as at the last checkpoint: same starting commit, and
                every changed file with the same SHA-256 hash, so the
                harness's own edits are there and nothing else changed
  uncertain     no action that may have changed files was cut off

Problems come in two kinds:
- blocking: project, task, limits, session state. Resuming would mean running
  under other permissions or rules than the run was started with. These must
  be fixed first (e.g. restore the task file); nothing overrides them.
- decision: the working copy differs from the checkpoint, or an action that
  may have changed files was cut off. Only the user can judge these. They
  pause the session unless the user accepts the current state explicitly
  (--accept-current-state).

Any problem not accepted pauses the session, with the reasons. An uncertain
action is never replayed automatically, also after accepting: the model gets a
note from the harness instead and decides what to check or redo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.config import HarnessConfig, LimitsConfig, ProjectConfig, Task, load_task
from harness.session import CANCELLED, INTERRUPTED, PAUSED, Session, fingerprint
from harness.workspace import WorkspaceError

RESUMABLE = (CANCELLED, INTERRUPTED, PAUSED)
# Actions that cannot have changed anything; cut off, they only lose their result.
READ_ONLY = {"list_files", "read_file", "search", "find_symbol", "outline", "dependencies",
             "git_status", "git_diff"}


@dataclass
class ResumeCheck:
    blocking: list[str] = field(default_factory=list)  # must be fixed first
    decisions: list[str] = field(default_factory=list)  # need --accept-current-state
    notes: list[str] = field(default_factory=list)  # for the model, after resuming
    passed: list[str] = field(default_factory=list)  # what was checked and found fine
    changed_files: list[str] = field(default_factory=list)  # differ from the checkpoint
    task: Task | None = None
    project: ProjectConfig | None = None
    limits: LimitsConfig | None = None

    def can_resume(self, accept_current_state: bool) -> bool:
        return not self.blocking and (accept_current_state or not self.decisions)


def check_resume(session: Session, config: HarnessConfig) -> ResumeCheck:
    """Everything that must hold before this session may continue."""
    check = ResumeCheck()
    status = session.status
    if status not in RESUMABLE:
        check.blocking.append(f"The session is {status}; only cancelled, interrupted or "
                              "paused sessions can be resumed.")
        return check
    check.passed.append(f"session is {status} and not in use")

    record = session.task_record()
    if record is None:
        check.blocking.append("The session has no task record (task.json).")
        return check
    saved = record.data
    if not check_project_and_task(saved, config, check):
        return check
    limits = check.limits

    # Limits: the same as at the start, and something left.
    if asdict(limits) != saved["limits"]:
        diffs = [k for k in saved["limits"] if saved["limits"][k] != asdict(limits).get(k)]
        check.blocking.append("The limits changed since the run started: " + ", ".join(diffs) + ".")
    else:
        counters = session.data["counters"]
        used_up = []
        if counters["actions"] >= limits.max_actions:
            used_up.append(f"actions ({counters['actions']} of {limits.max_actions})")
        if counters["denied"] > limits.max_denied_actions:
            used_up.append(f"refused actions ({counters['denied']} of {limits.max_denied_actions})")
        if counters["fix_rounds"] > limits.max_fix_rounds:
            used_up.append(f"fix rounds ({counters['fix_rounds']} of {limits.max_fix_rounds})")
        if used_up:
            check.blocking.append("Limits used up: " + ", ".join(used_up) + ".")
        else:
            check.passed.append(f"limits unchanged; {limits.max_actions - counters['actions']} "
                                f"actions and {limits.max_fix_rounds - counters['fix_rounds']} "
                                "fix rounds left")

    # Working copy: exactly as at the last checkpoint, including the harness's edits.
    repo = Path(session.data["repo"])
    try:
        current = fingerprint(repo)
    except (WorkspaceError, OSError) as exc:
        check.blocking.append(f"The working copy cannot be read: {exc}")
        return check
    saved_copy = session.data["working_copy"]
    if current["head"] != saved_copy["head"]:
        check.blocking.append("The working copy is at another commit than at the checkpoint.")
    else:
        differences = _compare(saved_copy["files"], current["files"])
        if differences:
            check.changed_files = sorted(differences)
            check.decisions.append(
                "The working copy differs from the last checkpoint: "
                + "; ".join(f"{path} ({what})" for path, what in sorted(differences.items()))
                + ".")
        else:
            changed_count = len(saved_copy["files"])
            check.passed.append(f"working copy matches the checkpoint ({changed_count} changed "
                                "file(s), including the harness's own edits)")

    # An action that was cut off.
    uncertain = session.uncertain_action
    if uncertain:
        what = describe(uncertain)
        if uncertain.get("tool") in READ_ONLY:
            check.notes.append(f"{what} was cut off. It only reads, so nothing changed; its "
                               "result is lost.")
        else:
            check.decisions.append(f"{what} was cut off; it is not known whether it completed. "
                                   "It will not be repeated automatically.")
            check.notes.append(f"{what} was in progress when the run stopped. It is not known "
                               "whether it completed, and it was not repeated. Check the "
                               "current state (git_diff, read_file) before relying on it.")
    return check


def check_project_and_task(saved: dict, config: HarnessConfig, check: ResumeCheck) -> bool:
    """Is the project still allowed, and is the task as it was when the run started?

    saved is the task record's data. Fills check.task, check.project and
    check.limits, and adds blocking problems. Used by resume and by review:
    neither may run checks under other rules than the run had. Returns False
    if the task or project cannot be used at all.
    """
    from harness.runner import check_task, effective_limits  # runner imports this module

    try:
        project = config.project(saved["project"]["name"])
    except ValueError as exc:
        check.blocking.append(f"Project: {exc}")
        return False
    if (project.url, project.commit) != (saved["project"]["url"], saved["project"]["commit"]):
        check.blocking.append(f"Project {project.name} now points to another repository or "
                              "commit than when the run started.")
        return False
    check.project = project
    check.passed.append(f"project {project.name} is allowed, same commit {project.commit[:12]}")

    try:
        task = load_task(Path(saved["task_file"]))
        check_task(task, project)
        check.limits = effective_limits(task, config.limits)
    except (ValueError, OSError) as exc:
        check.blocking.append(f"Task file {saved['task_file']}: {exc}")
        return False
    now_task = {
        "request": task.description,
        "allowed_changes": list(task.editable_files),
        "agent checks": list(task.agent_checks),
        "final checks": list(task.final_checks),
        "check commands": {n: project.checks[n]
                           for n in dict.fromkeys([*task.agent_checks, *task.final_checks])},
        "acceptance folder": task.acceptance_dir.name if task.acceptance_dir else None,
    }
    then_task = {
        "request": saved["request"],
        "allowed_changes": saved["allowed_changes"],
        "agent checks": saved["checks"]["agent"],
        "final checks": saved["checks"]["final"],
        "check commands": saved["checks"]["commands"],
        "acceptance folder": saved["acceptance_tests"],
    }
    changed = [name for name in now_task if now_task[name] != then_task[name]]
    if changed:
        check.blocking.append("The task changed since the run started: "
                              + ", ".join(changed) + ". Restore the task file to resume.")
    else:
        check.passed.append("task unchanged: request, allowed changes, checks, acceptance folder")
    check.task = task
    return True


def resume_note(session: Session, check: ResumeCheck, accepted: bool) -> str:
    """The harness's message to the model when a session continues (trusted text).

    Built only from harness data: tool names, checked paths, counts.
    """
    counters, limits = session.data["counters"], check.limits
    status = session.status
    if status == PAUSED:
        status = session.data.get("paused_from") or PAUSED  # why it stopped in the first place
    reason = {CANCELLED: "it was cancelled", INTERRUPTED: "its process ended unexpectedly",
              PAUSED: "it was paused"}.get(status, "it stopped")
    lines = [f"The run was stopped because {reason}, and the user has now resumed it. "
             f"Limits left: {limits.max_actions - counters['actions']} actions, "
             f"{limits.max_fix_rounds - counters['fix_rounds']} fix rounds."]
    lines += check.notes
    if accepted and check.changed_files:
        lines.append("The user accepted the current working copy, which differs from the "
                     "last checkpoint in: " + ", ".join(check.changed_files)
                     + ". Read these files before relying on earlier results.")
    lines.append("Tool calls from your last reply that have no result were not run. "
                 "Continue the task.")
    return "\n".join(lines)


def close_open_calls(messages: list[dict], uncertain: dict | None) -> list[dict]:
    """Give every tool call of the last reply a result, so the conversation is complete.

    The first call without a result is the one that was cut off (calls run in
    order); any later ones never ran.
    """
    last = next((i for i in range(len(messages) - 1, -1, -1)
                 if messages[i].get("role") == "assistant"), None)
    if last is None or not messages[last].get("tool_calls"):
        return []
    calls = messages[last]["tool_calls"]
    answered = sum(1 for m in messages[last + 1:] if m.get("role") == "tool")
    added = []
    for i, call in enumerate(calls[answered:]):
        name = str(call.get("function", {}).get("name", "tool"))[:60]
        if i == 0 and uncertain:
            text = "Interrupted: the run stopped while this call ran. Its result is unknown."
        else:
            text = "Not run: the run stopped before this call."
        added.append({"role": "tool", "tool_name": name, "content": text})
    return added


def describe(action: dict) -> str:
    target = f" {action['target']}" if action.get("target") else ""
    if action.get("step") is not None:
        return f"Step {action['step']} ({action['tool']}{target})"
    return f"{action['tool']}{target}"


def _compare(saved: dict, current: dict) -> dict[str, str]:
    """path -> what differs, between two fingerprints' file hashes."""
    differences = {}
    for path in saved.keys() | current.keys():
        before, after = saved.get(path, "unchanged"), current.get(path, "unchanged")
        if before == after:
            continue
        if path not in current:
            differences[path] = "change undone"
        elif path not in saved:
            differences[path] = "changed since the checkpoint" if after else "deleted since the checkpoint"
        else:
            differences[path] = "changed since the checkpoint"
    return differences
