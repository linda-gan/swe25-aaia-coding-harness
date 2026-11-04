"""Tests for the verification extras of step 9.

- lint is judged against the starting code (harness/baseline.py): only new
  findings count, and without a baseline the check is unavailable, never passed;
- the edit-test-fix loop uses that: a new lint warning fails the finish attempt,
  and the agent can fix it in a fix round;
- "unfinished work" lists what is still open, from the harness's own records;
- `review` runs the final checks again on a stopped session, without changing it.
"""
import functools

import pytest

from harness.baseline import BaselineSandbox, capture, compare, findings
from harness.cli import main, session_unfinished
from harness.config import PROJECT_ROOT, load_config
from harness.model import ScriptedModelClient, tool_reply
from harness.runner import review_session, run_task
from harness.sandbox import CheckResult
from harness.session import CANCELLED, PASSED, SessionError, SessionStore
from harness.task_record import TaskRecord
from harness.verification import unfinished_work
from harness.workspace import create_run_copy
from tests.test_resume import EDIT_A, FINISH, READ, CountingSandbox, Env

# Real pyflakes output on Cosmic Python's starting code (2 of its 7 findings).
PYFLAKES = """\
src/allocation/domain/model.py:5:1: '.commands' imported but unused
src/allocation/service_layer/handlers.py:3:1: 'typing.TYPE_CHECKING' imported but unused
"""


def result(name="lint", output=PYFLAKES, exit_code=1, error=None, timed_out=False):
    return CheckResult(name, ["python", "-m", "pyflakes", "src"], exit_code, output,
                       timed_out, 0.1, "fake", error=error)


# ---- findings and the comparison -------------------------------------------------

def test_findings_are_file_and_message_without_the_line_number():
    assert findings(PYFLAKES + "some other line\n\n") == [
        "src/allocation/domain/model.py: '.commands' imported but unused",
        "src/allocation/service_layer/handlers.py: 'typing.TYPE_CHECKING' imported but unused",
    ]


def test_the_starting_findings_alone_pass():
    # pyflakes itself exits 1 here; judged against the starting code it passes.
    compared = compare(result(), findings(PYFLAKES))
    assert compared.status == "passed"
    assert compared.output.splitlines()[-1] == "2 before, 2 now, 0 new (compared with the starting code)"
    assert "New findings" not in compared.output


def test_a_new_finding_fails_and_is_listed_first():
    output = PYFLAKES + "src/allocation/domain/model.py:9:1: 'os' imported but unused\n"
    compared = compare(result(output=output), findings(PYFLAKES))
    assert compared.status == "failed"
    lines = compared.output.splitlines()
    assert lines[:2] == ["New findings:", "  src/allocation/domain/model.py: 'os' imported but unused"]
    assert lines[-1] == "2 before, 3 now, 1 new (compared with the starting code)"


def test_moved_lines_are_not_new_but_a_second_copy_is():
    moved = PYFLAKES.replace(":5:1:", ":42:1:")
    assert compare(result(output=moved), findings(PYFLAKES)).status == "passed"
    twice = PYFLAKES + "src/allocation/domain/model.py:50:1: '.commands' imported but unused\n"
    assert compare(result(output=twice), findings(PYFLAKES)).status == "failed"


def test_fixing_an_old_finding_is_fine():
    fewer = PYFLAKES.splitlines()[0] + "\n"
    compared = compare(result(output=fewer), findings(PYFLAKES))
    assert compared.status == "passed"
    assert compared.output.endswith("2 before, 1 now, 0 new (compared with the starting code)")


def test_without_a_baseline_the_check_is_unavailable_never_passed():
    compared = compare(result(output="", exit_code=0), None)
    assert compared.status == "unavailable"
    assert "No baseline" in compared.error


def test_a_check_that_could_not_run_stays_as_it_is():
    broken = result(error="docker: image not found", exit_code=None)
    assert compare(broken, []) is broken
    slow = result(timed_out=True)
    assert compare(slow, []) is slow


def test_capture_keeps_none_for_a_check_that_could_not_run():
    class Sandbox:
        def run_check(self, name):
            return result(name, error="no docker") if name == "broken" else result(name)

    assert capture(Sandbox(), ["lint", "broken"]) == {"lint": findings(PYFLAKES), "broken": None}


def test_baseline_sandbox_passes_other_checks_through_unchanged():
    tests = result("unit_tests", output="5 passed", exit_code=0)

    class Sandbox:
        def run_check(self, name):
            return tests if name == "unit_tests" else result(name)

    wrapped = BaselineSandbox(Sandbox(), {"lint": findings(PYFLAKES)})
    assert wrapped.run_check("unit_tests") is tests
    assert wrapped.run_check("lint").status == "passed"


# ---- config ------------------------------------------------------------------------

def test_baseline_checks_must_name_checks_of_the_project(tmp_path):
    text = (PROJECT_ROOT / "config" / "target.toml").read_text()
    path = tmp_path / "config.toml"
    path.write_text(text.replace('baseline_checks = ["lint"]', 'baseline_checks = ["style"]'))
    with pytest.raises(ValueError, match="baseline_checks must list names from its checks"):
        load_config(path)


# ---- lint in a run -------------------------------------------------------------------

ADD_IMPORT = tool_reply("edit_file", path="src/app/model.py",
                        old="def can_allocate", new="import os\n\n\ndef can_allocate")
DROP_IMPORT = tool_reply("edit_file", path="src/app/model.py", old="import os\n\n\n", new="")
LINT_TASK = '''
project = "cosmicpython"
description = "Reject quantities of zero or less."
editable_files = ["src/app/model.py"]
agent_checks = ["lint"]
final_checks = ["unit_tests", "lint"]
'''


OLD_FINDING = "src/app/legacy.py:1:1: 'sys' imported but unused"


class LintSandbox(CountingSandbox):
    """unit_tests as in CountingSandbox; lint reports each unused 'import x' like pyflakes,
    plus one old finding that is already in the starting code (so plain lint always fails)."""
    lint_error = None

    def run_check(self, name):
        if name != "lint":
            return super().run_check(name)
        CountingSandbox.ran.append(name)
        if LintSandbox.lint_error:
            return CheckResult(name, ["pyflakes"], None, "", False, 0.1, "fake",
                               error=LintSandbox.lint_error)
        lines = (self.repo / "src/app/model.py").read_text().splitlines()
        found = [OLD_FINDING] + [f"src/app/model.py:{i}:1: '{line.split()[1]}' imported but unused"
                                 for i, line in enumerate(lines, 1) if line.startswith("import ")]
        return CheckResult(name, ["pyflakes"], 1, "\n".join(found), False, 0.1, "fake")


@pytest.fixture
def lint_env(tmp_path):
    env = Env(tmp_path)
    env.task_file.write_text(LINT_TASK)
    LintSandbox.lint_error = None
    return env


def run(env, replies, sandbox=LintSandbox):
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)
    report = run_task(task=env.task(), task_path=env.task_file, config=env.config,
                      project=env.project, model=ScriptedModelClient(replies),
                      model_name="scripted", run_copy=run_copy, sandbox_factory=sandbox)
    return report, run_copy


def test_the_baseline_is_taken_from_the_starting_code_and_saved(lint_env):
    report, run_copy = run(lint_env, [EDIT_A, FINISH])
    record = TaskRecord.load(run_copy.run_dir / "task.json")
    assert record.data["baselines"] == {"lint": ["src/app/legacy.py: 'sys' imported but unused"]}
    assert report.passed  # although pyflakes itself exits 1 because of the old finding
    lint = {c.name: c for c in report.verification.checks}["lint"]
    assert lint.output.endswith("1 before, 1 now, 0 new (compared with the starting code)")


def test_a_new_lint_warning_fails_the_finish_and_the_agent_fixes_it(lint_env):
    report, _ = run(lint_env, [EDIT_A, ADD_IMPORT, FINISH, DROP_IMPORT, FINISH])
    assert report.agent.stop_reason == "finished"
    assert report.agent.fix_rounds == 1
    assert report.passed


def test_a_new_lint_warning_left_in_fails_the_run(lint_env):
    report, _ = run(lint_env, [EDIT_A, ADD_IMPORT] + [FINISH] * 5)
    assert not report.passed
    lint = {c.name: c for c in report.verification.checks}["lint"]
    assert lint.status == "failed"
    assert "src/app/model.py: 'os' imported but unused" in lint.output


def test_without_a_baseline_lint_is_unavailable_and_the_run_does_not_pass(lint_env):
    LintSandbox.lint_error = "pyflakes is not installed in the image"
    report, run_copy = run(lint_env, [EDIT_A] + [FINISH] * 5)
    assert TaskRecord.load(run_copy.run_dir / "task.json").data["baselines"] == {"lint": None}
    assert not report.passed
    assert {c.name: c.status for c in report.verification.checks}["lint"] == "unavailable"


def test_tasks_without_baseline_checks_take_no_baseline(tmp_path):
    env = Env(tmp_path)  # unit_tests only
    report, run_copy = run(env, [EDIT_A, FINISH], sandbox=CountingSandbox)
    assert TaskRecord.load(run_copy.run_dir / "task.json").data["baselines"] == {}
    assert CountingSandbox.ran == ["unit_tests", "unit_tests"]  # no extra run at the start


# ---- unfinished work ---------------------------------------------------------------

PASSING = [{"name": "unit_tests", "status": "passed", "detail": "5 passed"}]


def test_nothing_unfinished_after_a_clean_finish():
    assert unfinished_work("finished", "", None, PASSING, []) == []


@pytest.mark.parametrize("args, expected", [
    (("cancelled", "Ctrl+C", None, None, []), "The run was cancelled (Ctrl+C)."),
    (("action limit reached", "30 of 30", None, PASSING, []),
     "The agent did not finish (action limit reached: 30 of 30)."),
    (("", "", None, None, []), "The agent loop did not end normally (the process stopped)."),
    (("cancelled", "", {"step": 4, "tool": "edit_file", "target": "src/a.py"}, None, []),
     "Step 4 (edit_file src/a.py) was cut off; it is not known whether it completed."),
    (("cancelled", "", None, None, []),
     "No final verification has run on the current state (python -m harness review <id> runs it)."),
    (("finished", "", None, [{"name": "lint", "status": "failed", "detail": "7 before, 8 now, 1 new"}], []),
     "Final check lint failed: 7 before, 8 now, 1 new"),
    (("finished", "", None, PASSING, ["setup.py"]),
     "Files changed outside the task's allowed changes: setup.py."),
])
def test_unfinished_work_items(args, expected):
    assert expected in unfinished_work(*args)


# ---- review ------------------------------------------------------------------------

def test_review_of_a_cancelled_session_checks_without_changing_its_status(tmp_path):
    env = Env(tmp_path)
    run_copy = env.start([READ, EDIT_A, READ])  # cancelled right after the edit
    session = SessionStore(env.runs).open(run_copy.run_id)
    before = session_unfinished(session)
    assert any("No final verification" in item for item in before)

    verification = review_session(session, env.config, sandbox_factory=CountingSandbox)
    assert verification.passed  # the edit is in the working copy

    session = SessionStore(env.runs).open(run_copy.run_id)
    assert session.status == CANCELLED
    assert [r["passed"] for r in session.data["reviews"]] == [True]
    after = session_unfinished(session, verification)
    assert any(item.startswith("The run was cancelled") for item in after)
    assert not any("No final verification" in item for item in after)


def test_a_review_is_no_longer_current_after_a_resume(tmp_path):
    env = Env(tmp_path)
    run_copy = env.start([READ, EDIT_A, READ])
    session = SessionStore(env.runs).open(run_copy.run_id)
    review_session(session, env.config, sandbox_factory=CountingSandbox)
    session = SessionStore(env.runs).open(run_copy.run_id)
    assert session.current_review["checks"] == [
        {"name": "unit_tests", "status": "passed", "detail": "ok"}]
    outcome, _ = env.resume(run_copy, [FINISH])
    assert outcome.report.passed
    session = SessionStore(env.runs).open(run_copy.run_id)
    assert session.current_review is None  # the run's new verification counts now
    assert session_unfinished(session) == []


def test_review_of_a_running_session_is_refused(tmp_path):
    env = Env(tmp_path)
    run_copy = env.start([READ, EDIT_A, READ])
    holder = SessionStore(env.runs).open(run_copy.run_id)
    with holder.hold():  # as if a run held the session
        holder.data["status"] = "running"
        holder.save()
        session = SessionStore(env.runs).open(run_copy.run_id)
        with pytest.raises(SessionError, match="is running"):
            review_session(session, env.config, sandbox_factory=CountingSandbox)
    assert CountingSandbox.ran == []


def test_review_of_a_session_in_use_is_refused(tmp_path):
    # E.g. a resume that is checking the session: the status is still cancelled,
    # but the lock is held, so a review must not run checks next to it.
    env = Env(tmp_path)
    run_copy = env.start([READ, EDIT_A, READ])
    holder = SessionStore(env.runs).open(run_copy.run_id)
    with holder.hold():
        session = SessionStore(env.runs).open(run_copy.run_id)
        assert session.status == CANCELLED
        with pytest.raises(SessionError, match="in use by another process"):
            review_session(session, env.config, sandbox_factory=CountingSandbox)
    assert CountingSandbox.ran == []


def test_review_refuses_a_changed_task(tmp_path):
    env = Env(tmp_path)
    run_copy = env.start([READ, EDIT_A, READ])
    env.task_file.write_text(env.task_file.read_text().replace("zero or less", "zero"))
    session = SessionStore(env.runs).open(run_copy.run_id)
    with pytest.raises(SessionError, match="The task changed since the run started: request"):
        review_session(session, env.config, sandbox_factory=CountingSandbox)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.setattr("harness.cli.load_config", lambda: env.config)
    monkeypatch.setattr("harness.cli.review_session",
                        functools.partial(review_session, sandbox_factory=CountingSandbox))
    return env


def test_cli_review_exit_codes_and_output(cli_env, capsys):
    env = cli_env
    run_copy = env.start([READ, EDIT_A, READ])
    runs = ["--runs-dir", str(env.runs)]
    assert main(["review", run_copy.run_id[-6:], *runs]) == 0
    out = capsys.readouterr().out
    assert "a review does not change it" in out
    assert "== Unfinished work" in out and "The run was cancelled" in out
    assert "REVIEW: PASSED" in out

    (run_copy.repo / "src/app/model.py").write_text("def can_allocate(qty):\n    return True\n")
    assert main(["review", run_copy.run_id, *runs]) == 1
    out = capsys.readouterr().out
    assert "REVIEW: FAILED" in out and "Final check unit_tests failed" in out

    assert main(["show", run_copy.run_id, *runs]) == 0
    out = capsys.readouterr().out
    assert "== Reviews" in out
    assert "PASSED  (unit_tests passed)" in out and "FAILED  (unit_tests failed)" in out
    # Unfinished work follows the latest review, not the older one.
    unfinished = out.split("== Unfinished work")[1]
    assert "Final check unit_tests failed" in unfinished
    assert "judged by the latest review" in unfinished


def test_cli_run_and_show_list_unfinished_work_after_a_finish(cli_env, capsys):
    env = cli_env
    run_copy = create_run_copy(env.commit, pristine=env.pristine, runs_dir=env.runs)
    report = run_task(task=env.task(), task_path=env.task_file, config=env.config,
                      project=env.project, model=ScriptedModelClient([EDIT_A, FINISH]),
                      model_name="scripted", run_copy=run_copy, sandbox_factory=CountingSandbox)
    assert report.passed
    session = SessionStore(env.runs).open(run_copy.run_id)
    assert session.status == PASSED
    assert session_unfinished(session) == []
    assert main(["show", run_copy.run_id, "--runs-dir", str(env.runs)]) == 0
    assert "(none: the agent finished" in capsys.readouterr().out


def test_cli_review_of_an_unknown_session_is_a_setup_error(cli_env, capsys):
    assert main(["review", "nope", "--runs-dir", str(cli_env.runs)]) == 2
