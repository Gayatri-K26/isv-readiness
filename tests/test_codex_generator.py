from __future__ import annotations

import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from isv_readiness.codex_generator import (
    CodexGeneratorError,
    _resolve_codex_executable,
    generate_with_codex,
    main,
)
from isv_readiness.generator_limits import CODEX_MODEL_TIMEOUT_SECONDS


class CodexGeneratorTests(unittest.TestCase):
    def test_main_returns_distinct_timeout_exit_code(self) -> None:
        error = io.StringIO()
        with (
            patch(
                "isv_readiness.codex_generator.generate_with_codex",
                side_effect=subprocess.TimeoutExpired(["codex"], CODEX_MODEL_TIMEOUT_SECONDS),
            ),
            patch("sys.stdin", io.StringIO("{}")),
            patch("sys.stderr", error),
        ):
            self.assertEqual(main([]), 124)

        self.assertIn(f"timed out after {CODEX_MODEL_TIMEOUT_SECONDS} seconds", error.getvalue())

    def test_runs_codex_ephemerally_read_only_with_output_schema(self) -> None:
        seen = {}
        expected = {"schema_version": "0.1.0", "changes": []}

        def runner(command, cwd: Path, prompt: str, timeout: int):
            seen.update(command=list(command), cwd=cwd, prompt=json.loads(prompt), timeout=timeout)
            seen["schema"] = json.loads(Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8"))
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
        self.assertEqual(seen["schema"], seen["prompt"]["output_schema"])
        self.assertEqual(seen["timeout"], CODEX_MODEL_TIMEOUT_SECONDS)

    def test_translates_full_schema_for_codex_and_removes_optional_nulls(self) -> None:
        seen = {}
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "nullable_required"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "version": {"const": "0.1.0"},
                "coverage": {"enum": ["covered", "unknown"]},
                "tags": {"type": "array", "uniqueItems": True, "default": [], "items": {"type": "string"}},
                "nullable_required": {"type": ["string", "null"]},
            },
        }

        def runner(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            generated_schema = json.loads(
                Path(command[command.index("--output-schema") + 1]).read_text(encoding="utf-8")
            )
            seen["schema"] = generated_schema
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(
                json.dumps(
                    {
                        "name": "BCM",
                        "version": None,
                        "coverage": None,
                        "tags": None,
                        "nullable_required": None,
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        result = generate_with_codex({"output_schema": schema}, runner=runner)

        self.assertEqual(result, {"name": "BCM", "nullable_required": None})
        self.assertEqual(
            seen["schema"]["required"],
            ["name", "version", "coverage", "tags", "nullable_required"],
        )
        self.assertNotIn("minLength", seen["schema"]["properties"]["name"])
        self.assertEqual(
            seen["schema"]["properties"]["version"]["anyOf"][0]["type"],
            "string",
        )
        self.assertEqual(
            seen["schema"]["properties"]["coverage"]["anyOf"][0]["type"],
            "string",
        )
        tags = seen["schema"]["properties"]["tags"]["anyOf"][0]
        self.assertNotIn("uniqueItems", tags)
        self.assertNotIn("default", tags)

    def test_rejects_missing_schema_nonzero_exit_and_missing_output(self) -> None:
        with self.assertRaisesRegex(CodexGeneratorError, "output_schema"):
            generate_with_codex({}, runner=lambda *args: None)  # type: ignore[arg-type]

        def failed(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 1, "", "authentication failed")

        with self.assertRaisesRegex(CodexGeneratorError, "authentication failed"):
            generate_with_codex({"output_schema": {}}, runner=failed)

        def verbose_failure(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 1, "", "user\n" + "x" * 3000 + "REAL ERROR")

        with self.assertRaisesRegex(CodexGeneratorError, "REAL ERROR"):
            generate_with_codex({"output_schema": {}}, runner=verbose_failure)

        def missing(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(CodexGeneratorError, "did not write"):
            generate_with_codex({"output_schema": {}}, runner=missing)

    def test_finds_codex_inside_macos_app_when_not_on_path(self) -> None:
        with (
            patch("isv_readiness.codex_generator.shutil.which", return_value=None),
            patch("isv_readiness.codex_generator.Path.is_file", return_value=True),
        ):
            resolved = _resolve_codex_executable("codex")

        self.assertEqual(resolved, "/Applications/Codex.app/Contents/Resources/codex")


if __name__ == "__main__":
    unittest.main()
