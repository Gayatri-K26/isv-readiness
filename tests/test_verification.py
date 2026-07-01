from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.verification import (
    VerificationError,
    apply_verified_candidate,
    load_verification_manifest,
    verify_fix_candidate,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _fixture_report(provider: Path) -> tuple[dict, str]:
    report = scan_provider(
        ScanOptions(
            provider_repo=provider,
            domains=["vm"],
            validation_root=FIXTURES / "ai-cloud-validation",
        )
    ).to_dict()
    selected = next(
        row
        for row in report["rows"]
        if row["step_name"] == "launch_instance" and row["validation_class"] == "InstanceCreatedCheck"
    )
    selected["enrichment"]["solution_profile"] = {"action": "implement_or_fix_adapter"}
    return report, selected["id"]


def _write_candidate(path: Path) -> None:
    path.write_text(
        """import json

print(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-verified"}))
""",
        encoding="utf-8",
    )


class VerificationTests(unittest.TestCase):
    def test_isolated_static_rescan_verifies_candidate_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            target = provider / "scripts" / "vm" / "launch_instance.py"
            original = target.read_text(encoding="utf-8")
            candidate = root / "candidate.py"
            _write_candidate(candidate)

            manifest = verify_fix_candidate(
                report,
                gap_id=gap_id,
                provider_repo=provider,
                candidate_path=candidate,
                validation_root=FIXTURES / "ai-cloud-validation",
            )

            self.assertTrue(manifest.success)
            self.assertEqual(manifest.selected_status_before, "not_implemented")
            self.assertEqual(manifest.selected_status_after, "pass")
            self.assertEqual(manifest.regressions, ())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            schema = json.loads((ROOT / "schemas" / "verification-manifest.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(manifest.to_dict())

    def test_verification_failure_is_recorded_when_gap_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            candidate = root / "candidate.py"
            candidate.write_text(
                'raise NotImplementedError("still missing")\n',
                encoding="utf-8",
            )

            manifest = verify_fix_candidate(
                report,
                gap_id=gap_id,
                provider_repo=provider,
                candidate_path=candidate,
                validation_root=FIXTURES / "ai-cloud-validation",
            )

            self.assertFalse(manifest.success)
            self.assertEqual(manifest.selected_status_after, "not_implemented")

    def test_verified_candidate_applies_atomically_with_backup_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            target = provider / "scripts" / "vm" / "launch_instance.py"
            original = target.read_text(encoding="utf-8")
            candidate = root / "candidate.py"
            _write_candidate(candidate)
            manifest = verify_fix_candidate(
                report,
                gap_id=gap_id,
                provider_repo=provider,
                candidate_path=candidate,
                validation_root=FIXTURES / "ai-cloud-validation",
            )
            manifest_path = root / "verification.json"
            manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

            result = apply_verified_candidate(
                report,
                gap_id=gap_id,
                provider_repo=provider,
                candidate_path=candidate,
                manifest=load_verification_manifest(manifest_path),
                backup_dir=root / "backups",
            )

            self.assertTrue(result.applied)
            self.assertEqual(target.read_text(encoding="utf-8"), candidate.read_text(encoding="utf-8"))
            self.assertIsNotNone(result.backup_path)
            assert result.backup_path is not None
            self.assertEqual(Path(result.backup_path).read_text(encoding="utf-8"), original)
            schema = json.loads((ROOT / "schemas" / "application-result.schema.json").read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(schema).validate(result.to_dict())

    def test_application_rejects_source_or_candidate_changed_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            candidate = root / "candidate.py"
            _write_candidate(candidate)
            manifest = verify_fix_candidate(
                report,
                gap_id=gap_id,
                provider_repo=provider,
                candidate_path=candidate,
                validation_root=FIXTURES / "ai-cloud-validation",
            )

            candidate.write_text('print("changed")\n', encoding="utf-8")
            with self.assertRaisesRegex(VerificationError, "verified patch|Candidate content changed"):
                apply_verified_candidate(
                    report,
                    gap_id=gap_id,
                    provider_repo=provider,
                    candidate_path=candidate,
                    manifest=manifest,
                    backup_dir=root / "backups",
                )

            _write_candidate(candidate)
            target = provider / "scripts" / "vm" / "launch_instance.py"
            target.write_text('print("source changed")\n', encoding="utf-8")
            with self.assertRaisesRegex(VerificationError, "verified patch|target changed"):
                apply_verified_candidate(
                    report,
                    gap_id=gap_id,
                    provider_repo=provider,
                    candidate_path=candidate,
                    manifest=manifest,
                    backup_dir=root / "backups",
                )

    def test_cli_requires_explicit_apply_flag(self) -> None:
        from isv_readiness.cli import main

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            exit_code = main(
                [
                    "apply",
                    "--in",
                    str(root / "gaps.json"),
                    "--gap-id",
                    "gap_0123456789ab",
                    "--provider-repo",
                    str(root / "provider"),
                    "--candidate",
                    str(root / "candidate.py"),
                    "--verification",
                    str(root / "verification.json"),
                    "--backup-dir",
                    str(root / "backups"),
                    "--out",
                    str(root / "application.json"),
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_cli_verify_and_explicit_apply_end_to_end(self) -> None:
        from isv_readiness.cli import main

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            report_path = root / "gaps.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            candidate = root / "candidate.py"
            _write_candidate(candidate)
            verification_path = root / "verification.json"
            application_path = root / "application.json"

            verify_exit = main(
                [
                    "verify",
                    "--in",
                    str(report_path),
                    "--gap-id",
                    gap_id,
                    "--provider-repo",
                    str(provider),
                    "--candidate",
                    str(candidate),
                    "--validation-root",
                    str(FIXTURES / "ai-cloud-validation"),
                    "--out",
                    str(verification_path),
                ]
            )
            apply_exit = main(
                [
                    "apply",
                    "--in",
                    str(report_path),
                    "--gap-id",
                    gap_id,
                    "--provider-repo",
                    str(provider),
                    "--candidate",
                    str(candidate),
                    "--verification",
                    str(verification_path),
                    "--backup-dir",
                    str(root / "backups"),
                    "--out",
                    str(application_path),
                    "--apply",
                ]
            )

            target = provider / "scripts" / "vm" / "launch_instance.py"
            self.assertEqual(verify_exit, 0)
            self.assertEqual(apply_exit, 0)
            self.assertTrue(verification_path.exists())
            self.assertTrue(application_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), candidate.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
