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
from isv_readiness.cli import _build_parser, main
from isv_readiness.generators import GeneratorSpec
from isv_readiness.journey import _pending_review, _resolve_generator, cmd_qualify, cmd_validate
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

    def test_public_cli_accepts_registered_names_paths_and_file_exchange(self) -> None:
        parser = _build_parser()
        registered = parser.parse_args(["qualify", "--generator", "internal-agent"])
        executable = parser.parse_args(["validate", "--generator", "/opt/custom-adapter"])
        exchange = parser.parse_args(
            ["validate", "--generator-response", "/tmp/agent-response.json"]
        )

        self.assertEqual(registered.generator, "internal-agent")
        self.assertEqual(executable.generator, "/opt/custom-adapter")
        self.assertEqual(exchange.generator_response, Path("/tmp/agent-response.json"))

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
                patch(
                    "isv_readiness.journey.build_qualify_catalog",
                    return_value={"domains": {"vm": {"checks": []}}},
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_qualify(confirm=lambda prompt: True)

            promoted = load_solution_profile(active_path)
            backup_exists = (proposal_dir / "solution-profile.initial.yaml").is_file()

        self.assertEqual(exit_code, 0)
        self.assertEqual(promoted.solution.profile_status, "reviewed")
        self.assertEqual(promoted.journey.stage, "validate")
        self.assertTrue(backup_exists)

    def test_qualify_prints_effective_check_counts_after_capability_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            active_path = manifest.parent / "solution-profile.yaml"
            active_raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            active_raw["solution"]["profile_status"] = "draft"
            active_raw["journey"] = {"stage": "qualify", "status": "in_progress"}
            active_path.write_text(yaml.safe_dump(active_raw, sort_keys=False), encoding="utf-8")

            proposal_raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            domain = proposal_raw["domains"][0]
            domain["coverage"] = "out_of_scope"
            domain["validation_mode"] = "skip"
            domain["capabilities"] = [
                {
                    "id": "vm-launch",
                    "name": "VM launch",
                    "selectors": {"steps": ["launch_instance"]},
                    "coverage": "covered",
                    "validation_mode": "test",
                }
            ]
            proposal_dir = manifest.parent / ".gapctl" / "qualification"
            proposal_dir.mkdir(parents=True)
            proposal_path = proposal_dir / "solution-profile.proposed.yaml"
            proposal_path.write_text(yaml.safe_dump(proposal_raw, sort_keys=False), encoding="utf-8")
            catalog = {
                "domains": {
                    "vm": {
                        "checks": [
                            {
                                "name": "VmLaunchCheck",
                                "check": "VmLaunchCheck",
                                "step": "launch_instance",
                                "category": "vm",
                            },
                            {
                                "name": "VmLaunchCheck-terminate",
                                "check": "VmLaunchCheck",
                                "step": "terminate_instance",
                                "category": "vm",
                            },
                        ]
                    }
                }
            }
            output = io.StringIO()

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey.build_qualify_catalog", return_value=catalog),
                redirect_stdout(output),
            ):
                exit_code = cmd_qualify(confirm=lambda prompt: False)

        self.assertEqual(exit_code, 1)
        self.assertIn("vm: coverage covered->out_of_scope, validation_mode test->skip", output.getvalue())
        self.assertIn(
            "vm: 1 covered/test, 1 out_of_scope/skip, 0 other (2 total)",
            output.getvalue(),
        )

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
                patch(
                    "isv_readiness.journey.build_qualify_catalog",
                    return_value={"domains": {"vm": {"checks": []}}},
                ),
                patch("isv_readiness.journey.build_qualify_pack", return_value={"project": {}}),
                patch(
                    "isv_readiness.journey.resolve_generator_spec",
                    return_value=GeneratorSpec(
                        name="internal",
                        command=("/opt/agent", "--mode", "strict"),
                        pass_env=("AGENT_TOKEN",),
                        timeout_seconds=7200,
                        idle_timeout_seconds=900,
                        max_request_bytes=8_000_000,
                    ),
                ),
                patch("isv_readiness.journey.run_profile_draft", return_value=draft) as generate,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_qualify(generator="internal", confirm=lambda prompt: False)

            proposal = manifest.parent / ".gapctl" / "qualification" / "solution-profile.proposed.yaml"
            proposal_exists = proposal.is_file()

        self.assertEqual(exit_code, 1)
        self.assertTrue(proposal_exists)
        sync.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["command"], ("/opt/agent", "--mode", "strict"))
        self.assertEqual(generate.call_args.kwargs["pass_env"], ("AGENT_TOKEN",))
        self.assertEqual(generate.call_args.kwargs["timeout_seconds"], 7200)
        self.assertEqual(generate.call_args.kwargs["idle_timeout_seconds"], 900)
        self.assertEqual(generate.call_args.kwargs["max_request_bytes"], 8_000_000)

    def test_qualify_export_writes_the_complete_agent_request(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            active_path = manifest.parent / "solution-profile.yaml"
            draft = yaml.safe_load(active_path.read_text(encoding="utf-8"))
            draft["solution"]["profile_status"] = "draft"
            draft["journey"] = {"stage": "qualify", "status": "in_progress"}
            active_path.write_text(yaml.safe_dump(draft, sort_keys=False), encoding="utf-8")
            pack = {
                "project": {"provider": "acme", "declared_domains": ["vm"]},
                "items": [{"source_id": "inference_ra", "content": "END-OF-COMPLETE-REFERENCE"}],
            }
            output = io.StringIO()

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey.sync_context_sources", return_value=()),
                patch("isv_readiness.journey.build_qualify_catalog", return_value={"domains": {"vm": {}}}),
                patch("isv_readiness.journey.build_qualify_pack", return_value=pack),
                redirect_stdout(output),
            ):
                exit_code = cmd_qualify(generator="export", confirm=lambda prompt: False)

            request_path = manifest.parent / ".gapctl" / "qualification" / "generator-request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(request["context_pack"]["items"][0]["content"], "END-OF-COMPLETE-REFERENCE")
        self.assertIn("output_schema", request)
        self.assertIn("--generator-response", output.getvalue())

    def test_generator_alias_prefers_the_running_gapctl_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            bin_dir = Path(tempdir) / "bin"
            bin_dir.mkdir()
            python = bin_dir / "python"
            generator = bin_dir / "gapctl-codex-generator"
            python.touch()
            generator.touch()

            with patch("isv_readiness.journey.sys.executable", str(python)):
                resolved = _resolve_generator("codex")

        self.assertEqual(resolved, str(generator))
        self.assertEqual(_resolve_generator("/opt/custom-generator"), "/opt/custom-generator")

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

    def test_validate_discards_a_declined_review_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            work = manifest.parent / ".gapctl" / "work" / "vm"
            (work / "scratch-provider").mkdir(parents=True)
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
            output = io.StringIO()

            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey.run_auto") as auto,
                redirect_stdout(output),
            ):
                exit_code = cmd_validate(confirm=lambda prompt: False)

            self.assertEqual(exit_code, 1)
            self.assertFalse((work / "auto-review.json").exists())
            self.assertFalse((work / "auto-review.patch").exists())
            self.assertTrue((work / "scratch-provider").is_dir())
            self.assertIn("rejected and discarded", output.getvalue())
            auto.assert_not_called()

    def test_validate_file_exchange_exports_then_imports_one_candidate_to_review(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            output = io.StringIO()
            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                redirect_stdout(output),
            ):
                exported = cmd_validate(generator="export", confirm=lambda prompt: False)

            request_path = manifest.parent / ".gapctl" / "generator-request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            gap = request["context_pack"]["gap"]
            content = (
                "import json\n\n"
                'print(json.dumps({"success": True, "platform": "fixture", "instance_id": "vm-agent"}))\n'
            )
            response = {
                "schema_version": "0.1.0",
                "gap_id": gap["id"],
                "context_pack_sha256": request["context_pack_sha256"],
                "generator": {"adapter": "manual-file", "model": None},
                "summary": "Implement the selected provider adapter",
                "changes": [
                    {
                        "target_root": "provider",
                        "path": gap["remediation"]["target"],
                        "operation": "replace",
                        "content": content,
                        "content_sha256": "0" * 64,
                        "rationale": "Return the pinned validation contract output",
                    }
                ],
            }
            response_path = manifest.parent / "agent-response.json"
            response_path.write_text(json.dumps(response), encoding="utf-8")
            output = io.StringIO()
            with (
                patch("isv_readiness.journey.find_project", return_value=manifest),
                patch("isv_readiness.journey._discard_pending_review"),
                redirect_stdout(output),
            ):
                imported = cmd_validate(
                    generator_response=response_path,
                    confirm=lambda prompt: False,
                )

            review = json.loads(
                (manifest.parent / ".gapctl" / "work" / "vm" / "auto-review.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(exported, 1)
        self.assertEqual(imported, 1)
        self.assertEqual(review["status"], "awaiting_review")
        self.assertIn("Statically verified vm candidate patch", output.getvalue())

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
