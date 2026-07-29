from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from isv_readiness.fixes import FixGuardrailError
from isv_readiness.generation import GeneratorInfrastructureError, dispatch_generator, run_generator
from isv_readiness.generator_limits import (
    CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS,
    CODEX_MODEL_TIMEOUT_SECONDS,
    GENERATOR_ADAPTER_TIMEOUT_SECONDS,
)


class GeneratorAdapterTests(unittest.TestCase):
    def test_callable_adapter_runs_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            adapter = workspace / "adapter.py"
            adapter.write_text(
                "import json, os\nprint(json.dumps({'cwd': os.getcwd()}))\n",
                encoding="utf-8",
            )

            output = dispatch_generator(
                {"output_schema": {}},
                command=[sys.executable, str(adapter)],
                cwd=workspace,
            )

            self.assertNotEqual(Path(output["cwd"]), workspace.resolve())
            self.assertFalse(Path(output["cwd"]).exists())

    def test_generator_timeout_is_classified_as_infrastructure_failure(self) -> None:
        def timed_out(command, cwd, request, environment, timeout):
            del cwd, request, environment
            raise subprocess.TimeoutExpired(command, timeout)

        with self.assertRaisesRegex(
            GeneratorInfrastructureError,
            f"timed out after {GENERATOR_ADAPTER_TIMEOUT_SECONDS} seconds",
        ):
            dispatch_generator(
                {"output_schema": {}},
                command=["fixture"],
                cwd=Path("/tmp"),
                runner=timed_out,
            )

    def test_generator_adapter_reports_nested_model_timeout(self) -> None:
        def timed_out(command, cwd, request, environment, timeout):
            del cwd, request, environment, timeout
            return subprocess.CompletedProcess(
                command,
                124,
                "",
                f"Claude model timed out after {CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS} seconds.",
            )

        with self.assertRaisesRegex(GeneratorInfrastructureError, "Claude model timed out"):
            dispatch_generator(
                {"output_schema": {}},
                command=["fixture"],
                cwd=Path("/tmp"),
                runner=timed_out,
            )

    def test_rejects_adapter_request_limit_without_truncating_context(self) -> None:
        request = {"complete_context": "x" * 100}
        with self.assertRaisesRegex(GeneratorInfrastructureError, "was not truncated"):
            dispatch_generator(
                request,
                command=["fixture"],
                cwd=Path("/tmp"),
                max_request_bytes=20,
                runner=lambda *args: self.fail("oversized request must not invoke the adapter"),
            )

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

    def test_generator_cannot_modify_protected_workspace_content(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            protected = Path(tempdir) / "provider"
            protected.mkdir()
            target = protected / "config.yaml"
            target.write_text("original\n", encoding="utf-8")

            def mutating_runner(command, cwd, request, environment, timeout):
                del cwd, request, environment, timeout
                target.write_text("bypassed review\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "{}", "")

            with self.assertRaisesRegex(FixGuardrailError, "modified protected workspace content"):
                dispatch_generator(
                    {"output_schema": {}},
                    command=["fixture"],
                    cwd=Path(tempdir),
                    runner=mutating_runner,
                    protected_roots=[protected],
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
            self.assertEqual(seen["request"]["agent_skill"]["name"], "isv-readiness-agent")
            self.assertEqual(seen["request"]["agent_skill"]["phase"], "remediation")
            self.assertIn("smallest provider-owned change", seen["request"]["agent_skill"]["instructions"])
            skill = " ".join(seen["request"]["agent_skill"]["instructions"].split())
            rules = "\n".join(seen["request"]["rules"])
            self.assertIn("declared runtime environment names", skill)
            self.assertIn("do not copy demo inputs", skill)
            self.assertIn("canonical resource identifier", skill)
            self.assertIn("Preserve lifecycle verbs", skill)
            self.assertIn("pre-existing resource", skill)
            self.assertIn("TLS peer verification", skill)
            self.assertIn("SSH host-key verification", skill)
            self.assertIn("general error field", skill)
            self.assertIn("configured step timeout", skill)
            self.assertIn("raw API bodies", skill)
            self.assertIn("selected configuration step", skill)
            self.assertIn("only authorized file", rules)
            self.assertIn("documented semantics", skill)
            self.assertIn("relabel", skill)
            self.assertIn("placeholders", skill)
            self.assertIn("not proof", skill)
            self.assertIn("complete resource set", skill)
            self.assertIn("same verified inventory", skill)
            self.assertIn("provider-derived subprocess arguments as untrusted", skill)
            self.assertIn("reviewed mapping as an implementation premise", skill)
            self.assertIn("Fail closed when a required element is not evidenced", skill)
            self.assertIn("bounded runner headroom", rules)
            self.assertIn("lifecycle_step_timeout_seconds=1200", rules)
            self.assertIn("Respect optional fields", skill)
            self.assertIn("standard verified client behavior", skill)
            self.assertIn("interactive console or shell", skill)
            self.assertIn("expected continuation", skill)
            self.assertIn("jump-host, proxy, or gateway topology", skill)
            self.assertIn("never substitute the intermediary", skill)
            self.assertNotIn("TLS peer verification", rules)
            self.assertIn("structurally incompatible", seen["request"]["task"])
            self.assertEqual(seen["timeout"], GENERATOR_ADAPTER_TIMEOUT_SECONDS)
            self.assertEqual(seen["environment"]["USER"], "operator")
            self.assertEqual(seen["environment"]["MODEL_API_KEY"], "available")
            self.assertNotIn("ACME_TOKEN", seen["environment"])

    def test_outer_timeout_contains_each_builtin_generator_route(self) -> None:
        self.assertGreaterEqual(
            GENERATOR_ADAPTER_TIMEOUT_SECONDS - CODEX_MODEL_TIMEOUT_SECONDS,
            120,
        )
        self.assertGreaterEqual(
            GENERATOR_ADAPTER_TIMEOUT_SECONDS - (2 * CLAUDE_MODEL_ATTEMPT_TIMEOUT_SECONDS),
            120,
        )

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
            skill = " ".join(seen["agent_skill"]["instructions"].split())
            self.assertIn("Do not fabricate credentials or reachability", skill)


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
