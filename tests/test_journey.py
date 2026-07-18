from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from isv_readiness.auto import AutoReview, AutoWorkflowError
from isv_readiness.cli import main
from isv_readiness.journey import _pending_review, cmd_qualify, cmd_validate
from isv_readiness.solution_profile import load_solution_profile
from tests.test_live import _project


def _review(status: str) -> AutoReview:
    return AutoReview(
        schema_version="0.1.0",
        domain="vm",
        status=status,
        patch="--- a/file\n+++ b/file\n" if status == "awaiting_review" else "",
        patch_sha256="a" * 64,
        staged=(),
        parked=(),
        changed_files=(),
        reason=status,
    )


class PublicJourneyTests(unittest.TestCase):
    def test_help_exposes_only_four_workflow_commands(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            main(["--help"])

        help_text = output.getvalue()
        for command in ("init", "qualify", "validate", "publish"):
            self.assertIn(command, help_text)
        for removed in ("bootstrap", "context-sync", "fill", "test", "status", "scan", "live-run"):
            self.assertNotIn(f"  {removed}", help_text)

    def test_removed_advanced_command_is_rejected(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            main(["scan"])

    def test_qualify_promotes_only_the_reviewed_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            active_path = manifest.parent / "solution-profile.yaml"
            active_raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            active_raw["solution"]["profile_status"] = "draft"
            active_raw["journey"] = {"stage": "qualify", "status": "in_progress"}
            active_path.write_text(yaml.safe_dump(active_raw, sort_keys=False), encoding="utf-8")

            proposal_dir = manifest.parent / ".gapctl" / "qualification"
            proposal_dir.mkdir(parents=True)
            proposal_path = proposal_dir / "solution-profile.proposed.yaml"
            proposal_path.write_text(yaml.safe_dump(active_raw, sort_keys=False), encoding="utf-8")

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_qualify(confirm=lambda prompt: True)

            promoted = load_solution_profile(active_path)
            backup_exists = (proposal_dir / "solution-profile.initial.yaml").is_file()

        self.assertEqual(exit_code, 0)
        self.assertEqual(promoted.solution.profile_status, "reviewed")
        self.assertEqual(promoted.journey.stage, "validate")
        self.assertTrue(backup_exists)

    def test_qualify_builds_missing_proposal_with_the_selected_generator(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            active_path = manifest.parent / "solution-profile.yaml"
            draft = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            draft["solution"]["profile_status"] = "draft"
            draft["journey"] = {"stage": "qualify", "status": "in_progress"}
            active_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey.sync_context_sources", return_value=()) as sync,
                patch("isv_readiness.journey.build_qualify_catalog", return_value={"domains": {"vm": {}}}),
                patch("isv_readiness.journey.build_qualify_pack", return_value={"project": {}}),
                patch("isv_readiness.journey.run_profile_draft", return_value=draft) as generate,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_qualify(generator="claude", confirm=lambda prompt: False)

            proposal = manifest.parent / ".gapctl" / "qualification" / "solution-profile.proposed.yaml"
            proposal_exists = proposal.is_file()

        self.assertEqual(exit_code, 1)
        self.assertTrue(proposal_exists)
        sync.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["command"], ["gapctl-claude-generator"])

    def test_validate_keeps_review_and_live_authorization_inside_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=False)
            reviews = [_review("awaiting_review"), _review("applied"), _review("no_changes")]

            def fake_test(live_project, project_path, domain):
                self.assertEqual(project_path, manifest)
                self.assertEqual(domain, "vm")
                self.assertTrue(live_project.execution.allow_live_runs)
                return 0

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey.run_auto", side_effect=reviews) as auto,
                patch("isv_readiness.journey.run_test_domain", side_effect=fake_test) as test_domain,
                patch("isv_readiness.journey.cmd_status", return_value=0) as status,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_validate(confirm=lambda prompt: True)

        self.assertEqual(exit_code, 0)
        self.assertFalse(project.execution.allow_live_runs)
        self.assertEqual(auto.call_count, 3)
        self.assertTrue(auto.call_args_list[1].kwargs["apply"])
        test_domain.assert_called_once()
        status.assert_called_once_with(project_path=manifest)

    def test_pending_review_rejects_patch_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            work = Path(tempdir)
            (work / "scratch-provider").mkdir()
            patch_text = "--- a/file\n+++ b/file\n"
            patch_hash = hashlib.sha256(patch_text.encode()).hexdigest()
            (work / "auto-review.patch").write_text(patch_text, encoding="utf-8")
            (work / "auto-review.json").write_text(
                json.dumps(
                    {
                        "schema_version": "0.1.0",
                        "domain": "vm",
                        "status": "awaiting_review",
                        "patch": patch_text,
                        "patch_sha256": patch_hash,
                        "reason": "review",
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNotNone(_pending_review(work, "vm"))
            (work / "auto-review.patch").write_text("different", encoding="utf-8")
            with self.assertRaises(AutoWorkflowError):
                _pending_review(work, "vm")


if __name__ == "__main__":
    unittest.main()
