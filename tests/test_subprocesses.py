from __future__ import annotations

import os
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from isv_readiness.subprocesses import run_captured


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
        process.communicate.assert_called_once_with("request", timeout=9)
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/bin"})

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_kills_and_reaps_the_process_group(self) -> None:
        timeout = subprocess.TimeoutExpired(["fixture"], 9)
        process = Mock(pid=456, returncode=-signal.SIGKILL)
        process.communicate.side_effect = [timeout, ("", "")]

        with (
            patch("isv_readiness.subprocesses.subprocess.Popen", return_value=process) as popen,
            patch("isv_readiness.subprocesses.os.killpg") as killpg,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            run_captured(
                ["fixture"],
                cwd=Path("/tmp"),
                input_text="request",
                timeout_seconds=9,
            )

        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(456, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)
