from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from isv_readiness.fixes import FixGuardrailError, build_fix_proposal


def _report(*, target: str = "scripts/vm/launch.py", action: str = "implement_or_fix_adapter") -> dict:
    return {
        "schema_version": "0.1.0",
        "provider_repo": "/provider",
        "domains": ["vm"],
        "isv_context": {
            "repo_access": "local",
            "api_spec": None,
            "run_env": "staging",
            "creds_scope": None,
        },
        "rows": [
            {
                "id": "gap_0123456789ab",
                "domain": "vm",
                "step_name": "launch_instance",
                "validation_class": "InstanceCreatedCheck",
                "requirement_id": None,
                "milestone": None,
                "status": "not_implemented",
                "detection": "static",
                "stage": "coverage",
                "gap_type": "not_implemented",
                "evidence": {
                    "message": "Provider script is not implemented.",
                    "validation_message": None,
                    "schema_errors": [],
                    "missing_json_fields": [],
                    "stderr_excerpt": None,
                    "script_path": target,
                    "config_path": "config/vm.yaml",
                },
                "remediation": {
                    "auto_fixable": True,
                    "target": target,
                    "rerun_command": "isvctl test run -f config/vm.yaml",
                    "aws_reference": None,
                },
                "enrichment": {
                    "solution_profile": {
                        "action": action,
                    }
                },
            }
        ],
    }


class FixProposalTests(unittest.TestCase):
    def test_emits_patch_without_modifying_provider_source(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            target = provider / "scripts" / "vm" / "launch.py"
            target.parent.mkdir(parents=True)
            target.write_text('raise NotImplementedError("TODO")\n', encoding="utf-8")
            candidate = root / "candidate.py"
            candidate.write_text('print({"success": True, "instance_id": "vm-1"})\n', encoding="utf-8")

            proposal = build_fix_proposal(
                _report(),
                gap_id="gap_0123456789ab",
                provider_repo=provider,
                candidate_path=candidate,
            )

            self.assertEqual(proposal.target, "scripts/vm/launch.py")
            self.assertFalse(proposal.creates_file)
            self.assertIn("--- a/scripts/vm/launch.py", proposal.patch)
            self.assertIn("+++ b/scripts/vm/launch.py", proposal.patch)
            self.assertIn('+print({"success": True, "instance_id": "vm-1"})', proposal.patch)
            self.assertEqual(target.read_text(encoding="utf-8"), 'raise NotImplementedError("TODO")\n')

    def test_rejects_unresolved_scope_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            provider.mkdir()
            candidate = root / "candidate.py"
            candidate.write_text("print('safe')\n", encoding="utf-8")

            with self.assertRaisesRegex(FixGuardrailError, "request_scope_decision"):
                build_fix_proposal(
                    _report(action="request_scope_decision"),
                    gap_id="gap_0123456789ab",
                    provider_repo=provider,
                    candidate_path=candidate,
                )
            with self.assertRaisesRegex(FixGuardrailError, "escapes provider"):
                build_fix_proposal(
                    _report(target="../outside.py"),
                    gap_id="gap_0123456789ab",
                    provider_repo=provider,
                    candidate_path=candidate,
                )

    def test_new_script_patch_uses_dev_null_without_creating_target(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            (provider / "scripts" / "vm").mkdir(parents=True)
            candidate = root / "candidate.py"
            candidate.write_text('print({"success": True})\n', encoding="utf-8")

            proposal = build_fix_proposal(
                _report(target="scripts/vm/new_probe.py"),
                gap_id="gap_0123456789ab",
                provider_repo=provider,
                candidate_path=candidate,
            )

            self.assertTrue(proposal.creates_file)
            self.assertIn("--- /dev/null", proposal.patch)
            self.assertIn("+++ b/scripts/vm/new_probe.py", proposal.patch)
            self.assertFalse((provider / "scripts" / "vm" / "new_probe.py").exists())

    def test_rejects_non_script_target_secrets_and_invalid_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            provider.mkdir()
            candidate = root / "candidate.py"
            candidate.write_text("print('safe')\n", encoding="utf-8")

            with self.assertRaisesRegex(FixGuardrailError, "scripts/ boundary"):
                build_fix_proposal(
                    _report(target="config/vm.yaml"),
                    gap_id="gap_0123456789ab",
                    provider_repo=provider,
                    candidate_path=candidate,
                )

            candidate.write_text('API_KEY = "super-secret-value"\n', encoding="utf-8")
            with self.assertRaisesRegex(FixGuardrailError, "secret-looking"):
                build_fix_proposal(
                    _report(),
                    gap_id="gap_0123456789ab",
                    provider_repo=provider,
                    candidate_path=candidate,
                )

            candidate.write_text("def broken(:\n", encoding="utf-8")
            with self.assertRaisesRegex(FixGuardrailError, "invalid .py syntax"):
                build_fix_proposal(
                    _report(),
                    gap_id="gap_0123456789ab",
                    provider_repo=provider,
                    candidate_path=candidate,
                )

    def test_cli_writes_patch_but_not_target(self) -> None:
        from isv_readiness.cli import main

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            target = provider / "scripts" / "vm" / "launch.py"
            target.parent.mkdir(parents=True)
            target.write_text('raise NotImplementedError("TODO")\n', encoding="utf-8")
            candidate = root / "candidate.py"
            candidate.write_text('print({"success": True})\n', encoding="utf-8")
            report_path = root / "gaps.json"
            report_path.write_text(json.dumps(_report()), encoding="utf-8")
            patch_path = root / "proposal.patch"

            exit_code = main(
                [
                    "fix",
                    "--in",
                    str(report_path),
                    "--gap-id",
                    "gap_0123456789ab",
                    "--provider-repo",
                    str(provider),
                    "--candidate",
                    str(candidate),
                    "--out",
                    str(patch_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(patch_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), 'raise NotImplementedError("TODO")\n')


if __name__ == "__main__":
    unittest.main()
