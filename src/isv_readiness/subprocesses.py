from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

WALL_TIMEOUT_POLL_SECONDS = 5.0
TERMINATION_GRACE_SECONDS = 5.0


class _TerminationRequested(BaseException):
    """Turn SIGTERM into a cleanup path before the process exits."""


def _raise_termination(_signum: int, _frame: object) -> None:
    raise _TerminationRequested()


def run_captured(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str,
    timeout_seconds: int,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one captured subprocess and clean up its process tree when interrupted."""

    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if environment is None else dict(environment),
        text=True,
        start_new_session=os.name == "posix",
    )
    previous_sigterm = None
    handle_sigterm = os.name == "posix" and threading.current_thread() is threading.main_thread()
    if handle_sigterm:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_termination)
    try:
        try:
            stdout, stderr = _communicate_with_wall_timeout(
                process,
                command=command,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            # Give an adapter a short SIGTERM window so it can stop a nested
            # model process in its own session before the adapter is killed.
            _stop_and_reap(process)
            raise
        except BaseException:
            _stop_and_reap(process)
            raise
    finally:
        if handle_sigterm:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _communicate_with_wall_timeout(
    process: subprocess.Popen[str],
    *,
    command: Sequence[str],
    input_text: str,
    timeout_seconds: int,
) -> tuple[str, str]:
    """Capture output with a deadline that also expires across macOS sleep."""

    deadline = time.time() + timeout_seconds
    pending_input: str | None = input_text
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(command), timeout_seconds)
        try:
            return process.communicate(
                pending_input,
                timeout=min(remaining, WALL_TIMEOUT_POLL_SECONDS),
            )
        except subprocess.TimeoutExpired as exc:
            # communicate may be resumed after a timeout, but stdin may be
            # supplied only on its first call.
            pending_input = None
            if time.time() >= deadline:
                raise subprocess.TimeoutExpired(
                    list(command),
                    timeout_seconds,
                    output=exc.output,
                    stderr=exc.stderr,
                ) from exc


def _stop_and_reap(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()

    try:
        process.communicate(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            process.communicate(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # A separately-sessioned orphan can keep inherited pipe handles
            # open even after the direct child dies. Never let cleanup block
            # the parent CLI forever on those handles.
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
