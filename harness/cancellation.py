"""Cancelling a run: Ctrl+C in its own terminal, or `cancel` from another one.

Both end up in a CancelToken. The harness only stops at safe points:

- While it waits for something it can drop (a model request, a running
  check), the wait is broken off at once. These waits are marked with
  token.interruptible(). A check broken off this way is cleaned up by the
  sandbox: its container is removed with `docker rm --force`, which kills
  every process in it, including background children.
- Anywhere else, for example while a file is being edited, the request is
  only noted. The controller looks at it before its next model request,
  action or finish check, and stops there. So a cancel never leaves a
  half-written file behind, and no further action or write happens.
- A second request (pressing Ctrl+C again) stops at once, as an escape hatch.

The signal handlers are installed only while a run is going, and only in
the main thread (Python allows signal handlers nowhere else).
"""
from __future__ import annotations

import os
import signal
import threading
from contextlib import contextmanager
from pathlib import Path


class Cancelled(BaseException):
    """The run was cancelled. A BaseException, like KeyboardInterrupt, so that
    `except Exception` blocks in the harness do not swallow it."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    def __init__(self):
        self.reason: str | None = None
        self._waiting = 0  # > 0 inside interruptible()

    @property
    def cancelled(self) -> bool:
        return self.reason is not None

    def request(self, reason: str) -> None:
        """Ask the run to stop. Raises at once while waiting, or on a second request."""
        repeated = self.reason is not None
        if not repeated:
            self.reason = reason
        if self._waiting or repeated:
            raise Cancelled(self.reason)

    def check(self) -> None:
        """Stop here if a cancel was requested. Called at safe points."""
        if self.reason is not None:
            raise Cancelled(self.reason)

    @contextmanager
    def interruptible(self):
        """A wait that a cancel may break off at once."""
        self.check()
        self._waiting += 1
        try:
            yield
        finally:
            self._waiting -= 1


class CancellableModel:
    """Wraps a model client: a cancel breaks off the request."""

    def __init__(self, model, token: CancelToken):
        self.model = model
        self.token = token

    def next_reply(self, messages, tools):
        with self.token.interruptible():
            return self.model.next_reply(messages, tools)


class CancellableSandbox:
    """Wraps a sandbox: a cancel breaks off a running check. The sandbox's own
    cleanup (in a finally block) then removes the container."""

    def __init__(self, sandbox, token: CancelToken):
        self.sandbox = sandbox
        self.token = token

    def run_check(self, name):
        with self.token.interruptible():
            return self.sandbox.run_check(name)


SIGNAL_REASONS = {signal.SIGINT: "Ctrl+C", signal.SIGTERM: "cancel command"}


@contextmanager
def handle_signals(token: CancelToken):
    """Route Ctrl+C (SIGINT) and `cancel` (SIGTERM) to the token while a run is going."""
    if threading.current_thread() is not threading.main_thread():
        yield  # signal handlers are only possible in the main thread
        return

    def handler(signum, frame):
        token.request(SIGNAL_REASONS.get(signum, f"signal {signum}"))

    previous = {sig: signal.signal(sig, handler) for sig in SIGNAL_REASONS}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def lock_holder(lock_file: Path) -> int | None:
    """The process ID that holds an flock on this file, from the Linux kernel.

    Read from /proc/locks, so it does not depend on any file a process could
    change. None if nobody holds the lock (or /proc/locks is not available).
    """
    try:
        st = os.stat(lock_file)
        with open("/proc/locks", encoding="ascii") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    for line in lines:
        parts = line.split()
        # e.g. "1: FLOCK  ADVISORY  WRITE 170 fe:00:1205615 0 EOF"
        if len(parts) >= 6 and parts[1] == "FLOCK" and parts[5] == key and parts[4].isdigit():
            return int(parts[4])
    return None
