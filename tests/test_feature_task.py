"""Tests for the Stage 2 feature task (batch expiry) and its acceptance check.

Three groups:
- always: the task file, the reference patch and the reference check logic,
  using a fake sandbox and a small Git repository;
- with the pristine clone (workspace/target-pristine): the reference patch
  applies cleanly to the real starting commit (Git only, no target code runs);
- with Docker and the cosmic-sandbox image: the real acceptance check fails
  on the starting code for the expected reason and passes with the reference
  solution, while the regression checks pass both times.
"""
import re
import shutil
import subprocess

import pytest

from harness.config import ACCEPTANCE_ROOT, PROJECT_ROOT, load_config, load_task
from harness.runner import check_task
from harness.sandbox import CheckResult
from harness.workspace import (
    PRISTINE_DIR, WorkspaceError, apply_patch, changed_files, create_run_copy, git,
)
from scripts.check_reference import check_reference, default_patch

TASK_PATH = PROJECT_ROOT / "tasks" / "batch_expiry.toml"
PATCH = PROJECT_ROOT / "reference" / "batch_expiry.patch"


def patched_files(patch_text: str) -> set[str]:
    return set(re.findall(r"^diff --git a/(\S+) b/", patch_text, re.M))


# ---- the task file --------------------------------------------------------

def test_feature_task_loads_and_its_checks_exist():
    task = load_task(TASK_PATH)
    check_task(task, load_config())  # no error
    assert task.acceptance_dir == (ACCEPTANCE_ROOT / "batch_expiry").resolve()
    assert task.final_checks == ["acceptance", "unit_tests", "integration_sqlite"]


def test_acceptance_check_stays_hidden_from_the_agent():
    task = load_task(TASK_PATH)
    assert "acceptance" not in task.agent_checks
    assert "integration_sqlite" in task.agent_checks  # covers the database mapping


def test_feature_changes_at_least_two_modules():
    assert len(load_task(TASK_PATH).editable_files) >= 2


def test_description_leaves_finding_the_modules_to_the_agent():
    # Only handlers.py is named, because InvalidExpiry must live there.
    description = load_task(TASK_PATH).description
    for module in ["commands.py", "model.py", "orm.py"]:
        assert module not in description


# ---- the reference solution -----------------------------------------------

def test_reference_patch_changes_exactly_the_editable_files():
    task = load_task(TASK_PATH)
    assert patched_files(PATCH.read_text()) == set(task.editable_files)


def test_reference_patch_is_outside_the_mounted_acceptance_folder():
    assert not PATCH.resolve().is_relative_to(ACCEPTANCE_ROOT.resolve())
    assert default_patch(TASK_PATH) == PATCH


# ---- apply_patch and the reference check, on a small repository ------------

ORIGINAL = "def can_allocate(qty):\n    return qty <= 10\n"
FIX = """\
--- a/src/app/model.py
+++ b/src/app/model.py
@@ -1,2 +1,2 @@
 def can_allocate(qty):
-    return qty <= 10
+    return 0 < qty <= 10
"""


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


def test_apply_patch_changes_the_run_copy(run_copy, tmp_path):
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX)
    apply_patch(run_copy.repo, patch)
    assert "0 < qty" in (run_copy.repo / "src/app/model.py").read_text()


def test_patch_that_does_not_fit_changes_nothing(run_copy, tmp_path):
    patch = tmp_path / "bad.patch"
    patch.write_text(FIX.replace("return qty <= 10", "return something_else"))
    with pytest.raises(WorkspaceError, match="apply"):
        apply_patch(run_copy.repo, patch)
    assert (run_copy.repo / "src/app/model.py").read_text() == ORIGINAL


def test_missing_patch_is_reported(run_copy, tmp_path):
    with pytest.raises(WorkspaceError, match="not found"):
        apply_patch(run_copy.repo, tmp_path / "nope.patch")


class FakeSandbox:
    """Acceptance passes only once the fix is in place."""

    def __init__(self, repo, *args, **kwargs):
        self.repo = repo

    def run_check(self, name):
        fixed = "0 < qty" in (self.repo / "src/app/model.py").read_text()
        passed = fixed or name != "acceptance"
        return CheckResult(name, ["pytest"], 0 if passed else 1,
                           "passed" if passed else "1 failed", False, 0.1, "fake")


class AlwaysPassingSandbox(FakeSandbox):
    def run_check(self, name):
        return CheckResult(name, ["pytest"], 0, "passed", False, 0.1, "fake")


def small_task(tmp_path):
    """A task file for the small repository, using the batch_expiry acceptance folder name."""
    path = tmp_path / "small.toml"
    path.write_text(
        'description = "Reject quantities of zero or less."\n'
        'editable_files = ["src/app/model.py"]\n'
        'acceptance_tests = "batch_expiry"\n'
        'agent_checks = ["unit_tests"]\n'
        'final_checks = ["acceptance", "unit_tests"]\n'
    )
    return path


def test_reference_check_is_ok_when_acceptance_fails_first_then_all_pass(run_copy, tmp_path):
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX)
    result = check_reference(small_task(tmp_path), patch, load_config(), run_copy,
                             sandbox_factory=FakeSandbox)
    assert result.acceptance_failed_before
    assert result.after.passed
    assert result.ok


def test_reference_check_flags_an_acceptance_check_that_passes_on_starting_code(run_copy, tmp_path):
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX)
    result = check_reference(small_task(tmp_path), patch, load_config(), run_copy,
                             sandbox_factory=AlwaysPassingSandbox)
    assert not result.acceptance_failed_before
    assert not result.ok


# ---- with the real pristine clone (Git only) ------------------------------

needs_pristine = pytest.mark.skipif(
    not (PRISTINE_DIR / ".git").is_dir(),
    reason="needs the pristine clone (./scripts/fetch_target.sh)",
)


@needs_pristine
def test_reference_patch_applies_to_the_starting_commit(tmp_path):
    run_copy = create_run_copy(load_config().target.commit, runs_dir=tmp_path / "runs")
    apply_patch(run_copy.repo, PATCH)
    changed = {path for _, path in changed_files(run_copy.repo)}
    assert changed == set(load_task(TASK_PATH).editable_files)


# ---- with Docker and the cosmic-sandbox image -----------------------------

def _sandbox_image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "image", "inspect", load_config().sandbox.image],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_sandbox = pytest.mark.skipif(
    not ((PRISTINE_DIR / ".git").is_dir() and _sandbox_image_available()),
    reason="needs the pristine clone, Docker and the cosmic-sandbox image",
)


@needs_sandbox
def test_real_acceptance_fails_on_starting_code_and_passes_with_reference(tmp_path):
    config = load_config()
    run_copy = create_run_copy(config.target.commit, runs_dir=tmp_path / "runs")
    result = check_reference(TASK_PATH, PATCH, config, run_copy)

    before = {c.name: c for c in result.before.checks}
    assert before["acceptance"].status == "failed"
    # The expected reason: the new field and exception do not exist yet.
    assert "unexpected keyword argument 'expires'" in before["acceptance"].output
    assert before["unit_tests"].passed
    assert before["integration_sqlite"].passed

    assert [c.status for c in result.after.checks] == ["passed", "passed", "passed"]
    assert result.after.scope.passed
    assert result.ok
