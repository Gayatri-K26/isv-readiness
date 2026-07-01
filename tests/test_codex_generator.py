from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from isv_readiness.codex_generator import CodexGeneratorError, generate_with_codex


class CodexGeneratorTests(unittest.TestCase):
    def test_runs_codex_ephemerally_read_only_with_output_schema(self) -> None:
        seen = {}
        expected = {"schema_version": "0.1.0", "changes": []}

        def runner(command, cwd: Path, prompt: str, timeout: int):
            seen.update(command=list(command), cwd=cwd, prompt=json.loads(prompt), timeout=timeout)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(expected), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "events", "")

        result = generate_with_codex(
            {"output_schema": {"type": "object"}, "context_pack": {"gap": {"id": "gap_test"}}},
            codex_executable="/opt/codex",
            model="test-model",
            runner=runner,
        )

        self.assertEqual(result, expected)
        self.assertEqual(seen["command"][:2], ["/opt/codex", "exec"])
        self.assertIn("--ephemeral", seen["command"])
        self.assertIn("--ignore-user-config", seen["command"])
        self.assertEqual(
            seen["command"][seen["command"].index("--sandbox") + 1],
            "read-only",
        )
        self.assertEqual(seen["command"][seen["command"].index("--model") + 1], "test-model")
        self.assertEqual(seen["command"][-1], "-")

    def test_rejects_missing_schema_nonzero_exit_and_missing_output(self) -> None:
        with self.assertRaisesRegex(CodexGeneratorError, "output_schema"):
            generate_with_codex({}, runner=lambda *args: None)  # type: ignore[arg-type]

        def failed(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 1, "", "authentication failed")

        with self.assertRaisesRegex(CodexGeneratorError, "authentication failed"):
            generate_with_codex({"output_schema": {}}, runner=failed)

        def missing(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(CodexGeneratorError, "did not write"):
            generate_with_codex({"output_schema": {}}, runner=missing)


if __name__ == "__main__":
    unittest.main()
