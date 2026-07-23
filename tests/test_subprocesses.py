from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from isv_readiness.subprocesses import _TerminationRequested, run_captured


class CapturedSubprocessTests(unittest.TestCase):
    def test_returns_captured_result(self) -> None:
        process = Mock(pid=123, returncode=0)
        process.communicate.return_value = ("output", "diagnostic")

        with patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process) as popen:
            result = run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
                environment={"PATH": "/bin"},
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "output")
        self.assertEqual(result.stderr, "diagnostic")
        process.communicate.assert_called_once_with("request", timeout=5.0)
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/bin"})

    def test_can_merge_stderr_into_stdout(self) -> None:
        process = Mock(pid=321, returncode=0)
        process.communicate.return_value = ("combined output", None)

        with patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process) as popen:
            result = run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="",
                timeout_seconds=9,
                merge_stderr=True,
            )

        self.assertEqual(result.stdout, "combined output")
        self.assertIsNone(result.stderr)
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.STDOUT)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_kills_and_reaps_the_process_group(self) -> None:
        timeout = subprocess.TimeoutExpired(["fixture"], 9)
        process = Mock(pid=456, returncode=-signal.SIGKILL)
        process.communicate.side_effect = [timeout, ("", "")]

        with (
            patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process) as popen,
            patch("isv_readiness.subprocesses.os.killpg") as killpg,
            patch("isv_readiness.subprocesses.time.time", side_effect=[0, 0, 10]),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
        )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(456, signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_escalates_when_the_process_group_ignores_sigterm(self) -> None:
        timeout = subprocess.TimeoutExpired(["fixture"], 5)
        process = Mock(pid=654, returncode=-signal.SIGKILL)
        process.communicate.side_effect = [timeout, timeout, ("", "")]

        with (
            patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process),
            patch("isv_readiness.subprocesses.os.killpg") as killpg,
            patch("isv_readiness.subprocesses.time.time", side_effect=[0, 0, 10]),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
            )

        self.assertEqual(
            killpg.call_args_list,
            [call(654, signal.SIGTERM), call(654, signal.SIGKILL)],
        )
        self.assertEqual(process.communicate.call_count, 3)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_keyboard_interrupt_terminates_and_reaps_the_process_group(self) -> None:
        process = Mock(pid=789, returncode=-signal.SIGTERM)
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]

        with (
            patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process),
            patch("isv_readiness.subprocesses.os.killpg") as killpg,
            self.assertRaises(KeyboardInterrupt),
        ):
            run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
            )

        killpg.assert_called_once_with(789, signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)
        process.communicate.assert_called_with(timeout=5)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_forwarded_sigterm_kills_and_reaps_a_nested_process_group(self) -> None:
        process = Mock(pid=987, returncode=-signal.SIGTERM)
        process.communicate.side_effect = [_TerminationRequested(), ("", "")]

        with (
            patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process),
            patch("isv_readiness.subprocesses.os.killpg") as killpg,
            self.assertRaises(_TerminationRequested),
        ):
            run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
            )

        killpg.assert_called_once_with(987, signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_real_timeout_reaps_a_nested_separately_sessioned_child(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            child_pid_path = Path(tempdir) / "child.pid"
            adapter = (
                "import os, signal, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
                "start_new_session=True)\n"
                f"Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
                "def stop(signum, frame):\n"
                "    os.killpg(child.pid, signal.SIGKILL)\n"
                "    child.wait()\n"
                "    raise SystemExit(128 + signum)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "while True: time.sleep(1)\n"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                run_captured(
                    [sys.executable, "-c", adapter],
                    cwd=Path(tempdir),
                    input_text="",
                    timeout_seconds=1,
                )

            child_pid = int(child_pid_path.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
