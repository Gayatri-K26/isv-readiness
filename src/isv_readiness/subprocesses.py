from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path


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
            stdout, stderr = process.communicate(input_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _stop_and_reap(process, signal.SIGKILL)
            raise
        except BaseException as exc:
            # An adapter receiving SIGTERM is itself cleaning up a nested model
            # process. Kill that nested group immediately so the caller can
            # reap the adapter without racing another grace period.
            stop_signal = signal.SIGKILL if isinstance(exc, _TerminationRequested) else signal.SIGTERM
            _stop_and_reap(process, stop_signal)
            raise
    finally:
        if handle_sigterm:
            signal.signal(signal.SIGTERM, previous_sigterm)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _stop_and_reap(process: subprocess.Popen[str], stop_signal: signal.Signals) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            pass
    elif stop_signal == signal.SIGKILL:
        process.kill()
    else:
        process.terminate()

    if stop_signal == signal.SIGKILL:
        process.communicate()
        return
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.communicate()
