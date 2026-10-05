"""Tests for the Docker sandbox.

The first group checks the docker command line and error handling and runs
everywhere. The second group starts real containers and is skipped when
Docker is not available. It uses the plain python:3.12-slim image, so it does
not depend on Cosmic Python or the cosmic-sandbox image.
"""
import shutil
import subprocess

import pytest

from harness.config import SandboxConfig
from harness.sandbox import DockerSandbox, SandboxError

TEST_IMAGE = "python:3.12-slim"


def make_config(**overrides):
    values = dict(
        image=TEST_IMAGE, timeout_seconds=30, memory="512m", cpus="1", max_output_chars=2000
    )
    values.update(overrides)
    return SandboxConfig(**values)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "hello.txt").write_text("hello\n")
    return root


@pytest.fixture
def acceptance(tmp_path):
    folder = tmp_path / "acceptance"
    folder.mkdir()
    return folder


# ---- no Docker needed ------------------------------------------------------

def test_docker_command_has_isolation_settings(repo, acceptance):
    sandbox = DockerSandbox(repo, make_config(), {}, acceptance_dir=acceptance)
    cmd = sandbox.build_create_command("harness-test", ["pytest", "-q"])
    joined = " ".join(cmd)

    assert cmd[-3:] == [TEST_IMAGE, "pytest", "-q"]
    assert "--network none" in joined
    assert "--pull never" in joined
    assert f"src={repo / '.git'},dst=/work/.git,readonly" in joined
    assert f"src={acceptance},dst=/acceptance,readonly" in joined


def test_unknown_check_is_refused_without_running_anything(repo):
    # A docker path that does not exist proves nothing was started.
    sandbox = DockerSandbox(repo, make_config(), {"unit": ["true"]}, docker="/nonexistent/docker")
    with pytest.raises(SandboxError, match="Unknown check 'rm_everything'"):
        sandbox.run_check("rm_everything")


def test_missing_docker_is_reported_as_unavailable(repo):
    sandbox = DockerSandbox(repo, make_config(), {"unit": ["true"]}, docker="/nonexistent/docker")
    result = sandbox.run_check("unit")
    assert result.status == "unavailable"
    assert not result.passed
    assert "not installed" in result.error


# ---- real containers -------------------------------------------------------

def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", TEST_IMAGE], capture_output=True, timeout=30
    )
    return probe.returncode == 0


needs_docker = pytest.mark.skipif(
    not _docker_ready(),
    reason=f"needs Docker running and the {TEST_IMAGE} image (docker pull {TEST_IMAGE})",
)


def run(repo, acceptance, argv, **config):
    sandbox = DockerSandbox(repo, make_config(**config), {"check": argv}, acceptance_dir=acceptance)
    return sandbox.run_check("check")


@needs_docker
def test_passing_command(repo, acceptance):
    result = run(repo, acceptance, ["cat", "/work/hello.txt"])
    assert result.passed
    assert result.exit_code == 0
    assert "hello" in result.output


@needs_docker
def test_failed_command_keeps_exit_code_and_output(repo, acceptance):
    script = "import sys; print('something broke'); sys.exit(3)"
    result = run(repo, acceptance, ["python", "-c", script])
    assert result.status == "failed"
    assert not result.passed
    assert result.exit_code == 3
    assert "something broke" in result.output


@needs_docker
def test_large_output_is_shortened(repo, acceptance):
    result = run(repo, acceptance, ["python", "-c", "print('x' * 50000)"], max_output_chars=1000)
    assert "[output shortened:" in result.output
    assert len(result.output) < 1100


@needs_docker
def test_stuck_command_is_stopped_with_its_child_processes(repo, acceptance):
    # A background child process plus a foreground one, both hanging.
    result = run(repo, acceptance, ["sh", "-c", "sleep 300 & sleep 300"], timeout_seconds=3)

    assert result.status == "timed out"
    assert not result.passed
    assert "time limit" in result.output
    # The container, and every process in it, is gone.
    remaining = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"name={result.container}"],
        capture_output=True, text=True,
    )
    assert remaining.stdout.strip() == ""


@needs_docker
def test_no_network_access(repo, acceptance):
    script = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    result = run(repo, acceptance, ["python", "-c", script])
    assert result.status == "failed"


@needs_docker
def test_git_folder_and_acceptance_tests_are_read_only(repo, acceptance):
    result = run(repo, acceptance, ["sh", "-c", "echo bad > /work/.git/hooks-planted"])
    assert result.status == "failed"
    assert not (repo / ".git" / "hooks-planted").exists()

    result = run(repo, acceptance, ["sh", "-c", "echo bad > /acceptance/test_fake.py"])
    assert result.status == "failed"
    assert not (acceptance / "test_fake.py").exists()


@needs_docker
def test_repository_copy_is_writable(repo, acceptance):
    result = run(repo, acceptance, ["sh", "-c", "echo changed > /work/hello.txt"])
    assert result.passed
    assert (repo / "hello.txt").read_text() == "changed\n"
