from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from isv_readiness.fixes import FixGuardrailError
from isv_readiness.generation import dispatch_generator, run_generator


class GeneratorAdapterTests(unittest.TestCase):
    def test_nonzero_generator_exit_reports_the_actual_error_tail(self) -> None:
        def failed(command, cwd, request, environment, timeout):
            del cwd, request, environment, timeout
            return subprocess.CompletedProcess(command, 2, "", "prompt\n" + "x" * 3000 + "REAL ERROR")

        with self.assertRaisesRegex(FixGuardrailError, "REAL ERROR"):
            dispatch_generator(
                {"output_schema": {}},
                command=["fixture"],
                cwd=Path("/tmp"),
                runner=failed,
            )

    def test_generator_receives_contract_and_returns_hash_bound_change_set(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            seen = {}

            def runner(command, cwd, request, environment, timeout):
                seen.update(
                    command=command, cwd=cwd, request=json.loads(request), environment=environment, timeout=timeout
                )
                content = "print({'success': True})\n"
                output = {
                    "schema_version": "0.1.0",
                    "gap_id": "gap_0123456789ab",
                    "context_pack_sha256": seen["request"]["context_pack_sha256"],
                    "generator": {"adapter": "fixture", "model": "frontier-test"},
                    "summary": "Implement VM launch",
                    "changes": [
                        {
                            "target_root": "provider",
                            "path": "scripts/vm/launch.py",
                            "operation": "replace",
                            "content": content,
                            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                            "rationale": "Call the provider API",
                        }
                    ],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

            change_set = run_generator(
                _context_pack(),
                command=["fixture-generator"],
                cwd=Path(tempdir),
                pass_env=["MODEL_API_KEY"],
                runner=runner,
                environment={"PATH": "/bin", "MODEL_API_KEY": "available", "ACME_TOKEN": "provider-secret"},
            )

            self.assertEqual(change_set.gap_id, "gap_0123456789ab")
            self.assertIn("output_schema", seen["request"])
            self.assertEqual(seen["environment"]["MODEL_API_KEY"], "available")
            self.assertNotIn("ACME_TOKEN", seen["environment"])

    def test_rejects_markdown_output_and_context_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:

            def markdown_runner(command, cwd, request, environment, timeout):
                del cwd, request, environment, timeout
                return subprocess.CompletedProcess(command, 0, "```json\n{}\n```", "")

            with self.assertRaisesRegex(FixGuardrailError, "one JSON object"):
                run_generator(_context_pack(), command=["fixture"], cwd=Path(tempdir), runner=markdown_runner)

            def mismatch_runner(command, cwd, request, environment, timeout):
                del cwd, request, environment, timeout
                content = "pass\n"
                raw = {
                    "schema_version": "0.1.0",
                    "gap_id": "gap_0123456789ab",
                    "context_pack_sha256": "0" * 64,
                    "generator": {"adapter": "fixture", "model": None},
                    "summary": "Mismatch",
                    "changes": [
                        {
                            "target_root": "provider",
                            "path": "scripts/vm/launch.py",
                            "operation": "replace",
                            "content": content,
                            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                            "rationale": "test",
                        }
                    ],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(raw), "")

            with self.assertRaisesRegex(FixGuardrailError, "not bound"):
                run_generator(_context_pack(), command=["fixture"], cwd=Path(tempdir), runner=mismatch_runner)


def _context_pack() -> dict:
    return {
        "schema_version": "0.1.0",
        "gap": {"id": "gap_0123456789ab", "domain": "vm"},
        "items": [],
    }


if __name__ == "__main__":
    unittest.main()
