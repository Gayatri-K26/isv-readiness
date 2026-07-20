from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.change_verification import (
    apply_verified_change_set,
    load_change_verification,
    rollback_change_application,
    verify_change_set,
)
from isv_readiness.changes import Change, ChangeSet, canonical_sha256
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.schema import load_schema
from isv_readiness.verification import VerificationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ChangeVerificationTests(unittest.TestCase):
    def test_multi_file_change_is_verified_in_isolation_and_applied_with_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            script = provider / "scripts" / "vm" / "launch_instance.py"
            config = provider / "config" / "vm.yaml"
            script_before = script.read_text(encoding="utf-8")
            config_before = config.read_text(encoding="utf-8")
            change_set = _change_set(gap_id, config_before)

            manifest = verify_change_set(
                report,
                provider_repo=provider,
                change_set=change_set,
                validation_root=FIXTURES / "ai-cloud-validation",
            )

            self.assertTrue(manifest.success)
            self.assertEqual(len(manifest.files), 2)
            self.assertEqual(script.read_text(encoding="utf-8"), script_before)
            self.assertEqual(config.read_text(encoding="utf-8"), config_before)
            verification_schema = load_schema("change-verification.schema.json")
            jsonschema.validate(manifest.to_dict(), verification_schema)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

            result = apply_verified_change_set(
                report,
                provider_repo=provider,
                change_set=change_set,
                manifest=load_change_verification(manifest_path),
                backup_dir=root / "backups",
            )

            self.assertTrue(result.applied)
            self.assertNotEqual(script.read_text(encoding="utf-8"), script_before)
            self.assertIn(
                "command: \"python ../scripts/vm/launch_instance.py\"\n        timeout: 61",
                config.read_text(encoding="utf-8"),
            )
            self.assertEqual(len(result.files), 2)
            self.assertTrue(all(item.backup_path for item in result.files))
            self.assertEqual(Path(result.files[0].backup_path).read_text(encoding="utf-8"), script_before)
            application_schema = load_schema("change-application.schema.json")
            jsonschema.validate(result.to_dict(), application_schema)

            rollback = rollback_change_application(result, provider_repo=provider)
            self.assertTrue(rollback.rolled_back)
            self.assertEqual(script.read_text(encoding="utf-8"), script_before)
            self.assertEqual(config.read_text(encoding="utf-8"), config_before)
            rollback_schema = load_schema("change-rollback.schema.json")
            jsonschema.validate(rollback.to_dict(), rollback_schema)

    def test_application_rejects_any_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            config = provider / "config" / "vm.yaml"
            change_set = _change_set(gap_id, config.read_text(encoding="utf-8"))
            manifest = verify_change_set(
                report,
                provider_repo=provider,
                change_set=change_set,
                validation_root=FIXTURES / "ai-cloud-validation",
            )
            config.write_text(config.read_text(encoding="utf-8") + "# external change\n", encoding="utf-8")

            with self.assertRaisesRegex(VerificationError, "changed after verification"):
                apply_verified_change_set(
                    report,
                    provider_repo=provider,
                    change_set=change_set,
                    manifest=manifest,
                    backup_dir=root / "backups",
                )

    def test_rollback_rejects_post_application_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            provider = root / "provider"
            shutil.copytree(FIXTURES / "provider_repo", provider)
            report, gap_id = _fixture_report(provider)
            config = provider / "config" / "vm.yaml"
            change_set = _change_set(gap_id, config.read_text(encoding="utf-8"))
            manifest = verify_change_set(
                report,
                provider_repo=provider,
                change_set=change_set,
                validation_root=FIXTURES / "ai-cloud-validation",
            )
            result = apply_verified_change_set(
                report,
                provider_repo=provider,
                change_set=change_set,
                manifest=manifest,
                backup_dir=root / "backups",
            )
            config.write_text(config.read_text(encoding="utf-8") + "# changed later\n", encoding="utf-8")

            with self.assertRaisesRegex(VerificationError, "changed after application"):
                rollback_change_application(result, provider_repo=provider)


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
    selected["enrichment"]["solution_profile"] = {
        "action": "implement_or_fix_adapter",
        "owned": True,
        "profile_status": "reviewed",
        "journey_stage": "validate",
    }
    return report, selected["id"]


def _change_set(gap_id: str, config_before: str) -> ChangeSet:
    script_content = (
        "import json\n\n"
        'print(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-change-set"}))\n'
    )
    config_content = config_before.replace("timeout: 60", "timeout: 61", 1)
    changes = tuple(
        Change(
            target_root="provider",
            path=path,
            operation="replace",
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            rationale="Resolve the selected VM launch validation",
        )
        for path, content in (
            ("scripts/vm/launch_instance.py", script_content),
            ("config/vm.yaml", config_content),
        )
    )
    return ChangeSet(
        schema_version="0.1.0",
        gap_id=gap_id,
        context_pack_sha256=canonical_sha256({"gap_id": gap_id}),
        generator={"adapter": "fixture", "model": None},
        summary="Implement VM launch and update its configured timeout",
        changes=changes,
    )


if __name__ == "__main__":
    unittest.main()
