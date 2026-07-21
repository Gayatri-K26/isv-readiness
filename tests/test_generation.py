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
                environment={
                    "PATH": "/bin",
                    "USER": "operator",
                    "MODEL_API_KEY": "available",
                    "ACME_TOKEN": "provider-secret",
                },
            )

            self.assertEqual(change_set.gap_id, "gap_0123456789ab")
            self.assertIn("output_schema", seen["request"])
            rules = "\n".join(seen["request"]["rules"])
            self.assertIn("runtime environment names declared by the project", rules)
            self.assertIn("canonical resource identifier", rules)
            self.assertIn("TLS peer verification", rules)
            self.assertIn("configured step timeout", rules)
            self.assertIn("raw API bodies", rules)
            self.assertIn("selected domain configuration", rules)
            self.assertIn("only authorized file", rules)
            self.assertIn("documented semantic contract", rules)
            self.assertIn("relabel a provider concept", rules)
            self.assertIn("only the selected step block", rules)
            self.assertIn("subprocess arguments as untrusted", rules)
            self.assertIn("leading hyphen", rules)
            self.assertIn("reviewed solution-profile capability mapping", rules)
            self.assertIn("fails closed on unsupported data", rules)
            self.assertIn("not by itself a reason to refuse", rules)
            self.assertIn("bounded orchestration headroom", rules)
            self.assertIn("Never shorten a provider lifecycle deadline", rules)
            self.assertIn("lifecycle_step_timeout_seconds=1200", rules)
            self.assertIn("explicit optional failure indicator", rules)
            self.assertIn("current remote SSH user", rules)
            self.assertIn("structurally incompatible", seen["request"]["task"])
            self.assertEqual(seen["timeout"], 900)
            self.assertEqual(seen["environment"]["USER"], "operator")
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

    def test_accepts_an_evidence_grounded_refusal_without_fabricating_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            seen = {}

            def runner(command, cwd, request, environment, timeout):
                del cwd, environment, timeout
                seen.update(json.loads(request))
                output = {
                    "schema_version": "0.1.0",
                    "gap_id": "gap_0123456789ab",
                    "context_pack_sha256": seen["context_pack_sha256"],
                    "generator": {"adapter": "fixture", "model": "frontier-test"},
                    "summary": "The provider exposes only a jump host, but the pinned check requires direct node SSH.",
                    "changes": [],
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

            change_set = run_generator(
                _context_pack(),
                command=["fixture-generator"],
                cwd=Path(tempdir),
                runner=runner,
            )

            self.assertEqual(change_set.changes, ())
            self.assertIn("direct node SSH", change_set.summary)
            self.assertIn("Never fabricate", "\n".join(seen["rules"]))


def _context_pack() -> dict:
    return {
        "schema_version": "0.1.0",
        "gap": {"id": "gap_0123456789ab", "domain": "vm"},
        "items": [
            {
                "kind": "api_spec",
                "trust": "authoritative",
                "content": ("runtime:\n  operation_timing:\n    lifecycle_step_timeout_seconds: 1200\n"),
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
