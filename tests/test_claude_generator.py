from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from isv_readiness.claude_generator import ClaudeGeneratorError, generate_with_claude

SCHEMA = {
    "type": "object",
    "required": ["schema_version", "changes"],
    "properties": {
        "schema_version": {"const": "0.1.0"},
        "changes": {"type": "array"},
    },
    "additionalProperties": True,
}


class ClaudeGeneratorTests(unittest.TestCase):
    def test_runs_claude_print_mode_with_tools_disallowed_in_empty_tempdir(self) -> None:
        seen = {}
        expected = {"schema_version": "0.1.0", "changes": []}

        def runner(command, cwd: Path, prompt: str, timeout: int):
            seen.update(
                command=list(command),
                cwd_was_empty=list(cwd.iterdir()) == [],
                prompt=prompt,
                timeout=timeout,
            )
            envelope = {"type": "result", "result": json.dumps(expected)}
            return subprocess.CompletedProcess(command, 0, json.dumps(envelope), "")

        result = generate_with_claude(
            {"output_schema": SCHEMA, "context_pack": {"gap": {"id": "gap_test"}}},
            claude_executable="/opt/claude",
            model="claude-opus-test",
            runner=runner,
        )

        self.assertEqual(result, expected)
        self.assertEqual(seen["command"][0], "/opt/claude")
        self.assertIn("-p", seen["command"])
        self.assertEqual(seen["command"][seen["command"].index("--output-format") + 1], "json")
        self.assertEqual(seen["command"][seen["command"].index("--max-turns") + 1], "1")
        self.assertEqual(seen["command"][seen["command"].index("--disallowedTools") + 1], "*")
        self.assertEqual(seen["command"][seen["command"].index("--model") + 1], "claude-opus-test")
        self.assertIn("gap_test", seen["prompt"])
        self.assertTrue(seen["cwd_was_empty"])

    def test_markdown_fenced_result_is_unwrapped(self) -> None:
        expected = {"schema_version": "0.1.0", "changes": []}

        def runner(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            fenced = "```json\n" + json.dumps(expected) + "\n```"
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": fenced}), "")

        result = generate_with_claude({"output_schema": SCHEMA}, runner=runner)
        self.assertEqual(result, expected)

    def test_schema_failure_feeds_errors_back_then_succeeds(self) -> None:
        prompts: list[str] = []
        good = {"schema_version": "0.1.0", "changes": []}

        def runner(command, cwd, prompt, timeout):
            del cwd, timeout
            prompts.append(prompt)
            if len(prompts) == 1:
                bad = {"schema_version": "9.9.9", "changes": []}
                return subprocess.CompletedProcess(command, 0, json.dumps({"result": json.dumps(bad)}), "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": json.dumps(good)}), "")

        result = generate_with_claude({"output_schema": SCHEMA}, runner=runner)
        self.assertEqual(result, good)
        self.assertEqual(len(prompts), 2)
        self.assertIn("rejected by the change-set schema validator", prompts[1])
        self.assertIn("schema_version", prompts[1])

    def test_rejects_missing_schema_nonzero_exit_and_persistent_invalid_output(self) -> None:
        with self.assertRaisesRegex(ClaudeGeneratorError, "output_schema"):
            generate_with_claude({}, runner=lambda *args: None)  # type: ignore[arg-type]

        def failed(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 1, "", "not logged in")

        with self.assertRaisesRegex(ClaudeGeneratorError, "not logged in"):
            generate_with_claude({"output_schema": SCHEMA}, runner=failed)

        def never_valid(command, cwd, prompt, timeout):
            del cwd, prompt, timeout
            return subprocess.CompletedProcess(command, 0, json.dumps({"result": "no json here"}), "")

        with self.assertRaisesRegex(ClaudeGeneratorError, "failed change-set schema validation"):
            generate_with_claude({"output_schema": SCHEMA}, runner=never_valid)


if __name__ == "__main__":
    unittest.main()
