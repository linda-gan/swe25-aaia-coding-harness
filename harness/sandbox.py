"""Run configured checks inside a Docker container.

Only commands listed under [checks] in the config can run, by name.
Each run gets a fresh container that:
  - has no network,
  - sees only the repository copy (/work) and, read-only, the acceptance tests,
  - cannot write to /work/.git, so code run by tests cannot plant Git hooks
    or config that would run on the host when the harness calls git,
  - runs as your normal user with no extra privileges,
  - is limited in memory, CPU and number of processes,
  - is removed when the command ends or the time limit is hit.

Each check runs in two phases: "docker create", then "docker start --attach"
with the time limit. The container therefore always exists by name before
the clock starts, so it can always be found and removed afterwards.

Killing the container stops every process inside it, including child
processes started by the tests, so a stuck command leaves nothing behind.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.config import SandboxConfig
from harness.output import shorten

class SandboxError(Exception):
    """A request the sandbox refuses, such as an unknown check name."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    command: list[str]
    exit_code: int | None  # None when the command never finished
    output: str
    timed_out: bool
    duration_seconds: float
    container: str
    error: str | None = None  # set when the check could not run at all

    @property
    def passed(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0

    @property
    def status(self) -> str:
        if self.error is not None:
            return "unavailable"
        if self.timed_out:
            return "timed out"
        return "passed" if self.exit_code == 0 else "failed"


class DockerSandbox:
    def __init__(
        self,
        repo_dir: Path | str,
        config: SandboxConfig,
        checks: dict[str, list[str]],
        acceptance_dir: Path | str | None = None,
        docker: str = "docker",
    ):
        self.repo_dir = Path(repo_dir).resolve()
        if not self.repo_dir.is_dir():
            raise ValueError(f"Repository copy does not exist: {self.repo_dir}")
        self.acceptance_dir = Path(acceptance_dir).resolve() if acceptance_dir else None
        self.config = config
        self.checks = checks
        self.docker = docker

    # ---- public ------------------------------------------------------------

    def run_check(self, name: str) -> CheckResult:
        """Run one configured check by name."""
        if name not in self.checks:
            allowed = ", ".join(sorted(self.checks)) or "none"
            raise SandboxError(f"Unknown check {name!r}. Allowed checks: {allowed}.")
        return self._run(name, self.checks[name])

    def build_create_command(self, container: str, argv: list[str]) -> list[str]:
        """The docker command that creates (but does not start) the container.

        Public so tests can inspect the isolation settings.
        """
        cmd = [
            self.docker, "create",
            "--name", container,
            "--pull", "never",              # use the local image, never download
            "--network", "none",            # no network access at all
            "--init",                       # proper signal handling for child processes
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", self.config.memory,
            "--cpus", self.config.cpus,
            "--read-only",                  # image filesystem is read-only
            "--tmpfs", "/tmp:rw,size=64m",  # scratch space that disappears afterwards
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-e", "PYTHONPATH=/work/src",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,src={self.repo_dir},dst=/work",
        ]
        git_dir = self.repo_dir / ".git"
        if git_dir.is_dir():
            cmd += ["--mount", f"type=bind,src={git_dir},dst=/work/.git,readonly"]
        if self.acceptance_dir is not None:
            cmd += ["--mount", f"type=bind,src={self.acceptance_dir},dst=/acceptance,readonly"]
        cmd += ["--workdir", "/work", self.config.image, *argv]
        return cmd

    # ---- internals ---------------------------------------------------------

    def _run(self, name: str, argv: list[str]) -> CheckResult:
        container = f"harness-{uuid.uuid4().hex[:12]}"
        limit = self.config.max_output_chars
        start = time.monotonic()

        def result(**kwargs) -> CheckResult:
            return CheckResult(
                name=name,
                command=list(argv),
                container=container,
                duration_seconds=time.monotonic() - start,
                **kwargs,
            )

        def unavailable(error: str, output: str = "") -> CheckResult:
            return result(exit_code=None, output=shorten(output, limit), timed_out=False, error=error)

        # Phase 1: create the container. Creating first means it exists before
        # the time limit starts, so a timeout can always find and remove it.
        try:
            created = subprocess.run(
                self.build_create_command(container, argv),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
            )
        except FileNotFoundError:
            return unavailable(f"Docker is not installed or not on PATH ({self.docker!r}).")
        except subprocess.TimeoutExpired:
            self.stop(container)
            return unavailable("Docker did not respond while creating the container.")
        if created.returncode != 0:
            return unavailable(
                "Docker could not create the container. Is Docker Desktop running "
                "and has the sandbox image been built?",
                _decode(created.stdout),
            )

        # Phase 2: start it, wait for it, and always remove it afterwards.
        try:
            proc = subprocess.run(
                [self.docker, "start", "--attach", container],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one stream, in the order it was printed
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = _decode(exc.output)
            output += f"\n[stopped: time limit of {self.config.timeout_seconds}s reached]"
            return result(exit_code=None, output=shorten(output, limit), timed_out=True)
        finally:
            # Removing the container kills every process in it, including
            # child processes the command started in the background.
            self.stop(container)

        return result(exit_code=proc.returncode, output=shorten(_decode(proc.stdout), limit), timed_out=False)

    def stop(self, container: str) -> None:
        """Kill and remove a container. Safe to call if it is already gone."""
        subprocess.run(
            [self.docker, "rm", "--force", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
