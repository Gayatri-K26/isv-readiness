from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from isv_readiness.live import LiveRunResult
from isv_readiness.project import ProjectError, load_project
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.simple import cmd_init, cmd_status, cmd_test
from tests.test_live import _project


class SimpleCommandTests(unittest.TestCase):
    def test_init_imports_context_as_part_of_the_single_command(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            project = MagicMock()
            project.assessment.domains = ("vm",)
            project.validation.resolved_commit = "a" * 40
            project.validation_root.return_value = workspace / "ai-cloud-validation"
            catalog = {"domains": {"vm": {"checks": []}}}
            with (
                patch("isv_readiness.simple.build_bootstrap_plan", return_value=MagicMock()),
                patch("isv_readiness.simple.execute_bootstrap", return_value=project),
                patch("isv_readiness.simple.build_qualify_catalog", return_value=catalog),
                patch("isv_readiness.simple.build_provider_onboarding_plan", return_value=MagicMock()),
                patch("isv_readiness.simple.execute_provider_onboarding", return_value=[]),
                patch("isv_readiness.simple.sync_context_sources", return_value=()) as sync,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = cmd_init(
                    "acme",
                    workspace=workspace,
                    domains=["vm"],
                    api_url=None,
                    auth_envs=[],
                    api_spec=None,
                )

            self.assertEqual(exit_code, 0)
            sync.assert_called_once_with(
                project,
                workspace.resolve() / "isv-project.yaml",
                workspace.resolve() / ".gapctl" / "context-cache",
            )

    def test_init_rejects_missing_local_api_spec_before_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            with (
                patch("isv_readiness.simple.execute_bootstrap") as execute,
                redirect_stderr(io.StringIO()),
            ):
                exit_code = cmd_init(
                    "acme",
                    workspace=workspace,
                    domains=["vm"],
                    api_url=None,
                    auth_envs=[],
                    api_spec=str(Path(tempdir) / "missing-openapi.yaml"),
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse(workspace.exists())
            execute.assert_not_called()

    def test_init_passes_requested_validation_ref_to_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            validation_root = Path(tempdir) / "existing-validation"
            architecture = Path(tempdir) / "architecture.md"
            architecture.write_text("# Architecture\n", encoding="utf-8")

            def stop_before_clone(plan, *, overwrite):
                self.assertEqual(plan.validation_ref, "release-1.2")
                self.assertEqual(plan.validation_root, validation_root.resolve())
                self.assertEqual(plan.context_inputs, (str(architecture.resolve()),))
                self.assertEqual(plan.api_base_url_env, "ISV_API_BASE_URL")
                self.assertEqual(plan.pass_env, ("ACME_REGION",))
                self.assertFalse(overwrite)
                raise ProjectError("stop before clone")

            with (
                patch("isv_readiness.simple.execute_bootstrap", side_effect=stop_before_clone),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    cmd_init(
                        "acme",
                        workspace=Path(tempdir) / "workspace",
                        domains=["vm"],
                        api_url="https://api.acme.invalid/v1",
                        auth_envs=[],
                        input_envs=["ACME_REGION"],
                        api_spec=None,
                        context_inputs=[str(architecture)],
                        validation_ref="release-1.2",
                        validation_root=validation_root,
                    ),
                    2,
                )

    def test_init_preserves_existing_provider_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            provider_root = workspace / "ai-cloud-validation" / "isvctl" / "configs" / "providers" / "acme"
            project = MagicMock()
            project.provider.state = "existing"
            project.provider_root.return_value = provider_root
            project.assessment.domains = ("vm",)
            project.validation.resolved_commit = "a" * 40
            project.validation_root.return_value = workspace / "ai-cloud-validation"
            catalog = {"domains": {"vm": {"checks": []}}}
            with (
                patch("isv_readiness.simple.build_bootstrap_plan", return_value=MagicMock()),
                patch("isv_readiness.simple.execute_bootstrap", return_value=project),
                patch("isv_readiness.simple.build_qualify_catalog", return_value=catalog),
                patch("isv_readiness.simple.build_provider_onboarding_plan") as build_onboarding,
                patch("isv_readiness.simple.execute_provider_onboarding") as execute_onboarding,
                patch("isv_readiness.simple.sync_context_sources", return_value=()),
                redirect_stdout(io.StringIO()) as output,
            ):
                exit_code = cmd_init(
                    "acme",
                    workspace=workspace,
                    domains=["vm"],
                    api_url=None,
                    auth_envs=[],
                    api_spec=None,
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Using existing provider implementation: {provider_root}", output.getvalue())
            build_onboarding.assert_not_called()
            execute_onboarding.assert_not_called()

    def test_status_handles_new_and_existing_gap_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, manifest = _project(Path(tempdir), allow_live=False)
            with patch("isv_readiness.simple.find_project", return_value=manifest):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cmd_status(), 1)
                self.assertIn("Gap Scorecard", output.getvalue())
                self.assertIn("Live validation still required for: vm", output.getvalue())

                gaps_path = manifest.parent / "gaps.json"
                legacy = json.loads(gaps_path.read_text(encoding="utf-8"))
                legacy["schema_version"] = "0.1.0"
                for row in legacy["rows"]:
                    row["milestone"] = None
                gaps_path.write_text(json.dumps(legacy), encoding="utf-8")

                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cmd_status(), 1)
                self.assertIn("current schema is '0.2.0'", output.getvalue())
                refreshed = json.loads(gaps_path.read_text(encoding="utf-8"))
                self.assertEqual(refreshed["schema_version"], "0.2.0")
                self.assertTrue(all("milestone" not in row for row in refreshed["rows"]))

    def test_test_records_empirical_run_and_keeps_full_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _, manifest = _project(root, allow_live=True)
            raw_project = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            raw_project["assessment"]["domains"] = ["vm", "network"]
            raw_project["assessment"]["profile"] = None
            manifest.write_text(yaml.safe_dump(raw_project, sort_keys=False), encoding="utf-8")

            def fake_live_run(project, project_path, *, domain, artifacts_dir, explicit_authorization):
                self.assertTrue(explicit_authorization)
                junit = artifacts_dir / "junit-vm.xml"
                log = artifacts_dir / "isvctl-vm.log"
                junit.write_text('<testsuite tests="1" />', encoding="utf-8")
                log.write_text("PASS\n", encoding="utf-8")
                current = scan_provider(
                    ScanOptions(
                        provider_repo=project.provider_root(project_path),
                        domains=[domain],
                        validation_root=project.validation_root(project_path),
                    )
                ).to_dict()
                dynamic = copy.deepcopy(current["rows"][0])
                dynamic.update(id="gap_dynamic", detection="dynamic", status="pass")
                current["rows"].append(dynamic)
                return LiveRunResult(
                    schema_version="0.1.0",
                    domain=domain,
                    config="isvctl/configs/providers/acme/config/vm.yaml",
                    selection=None,
                    command=("isvctl", "test", "run"),
                    exit_code=0,
                    junit_path=str(junit),
                    log_path=str(log),
                    selected_statuses=("pass",),
                    success=True,
                    report=current,
                )

            with (
                patch("isv_readiness.simple.find_project", return_value=manifest),
                patch("isv_readiness.simple.run_live_domain", side_effect=fake_live_run),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_test("vm"), 0)
                self.assertEqual(cmd_test("network"), 0)

            report = json.loads((manifest.parent / "gaps.json").read_text(encoding="utf-8"))
            self.assertEqual(report["domains"], ["vm", "network"])
            self.assertTrue(any(row["domain"] == "network" for row in report["rows"]))
            dynamic_domains = {row["domain"] for row in report["rows"] if row["detection"] == "dynamic"}
            self.assertEqual(dynamic_domains, {"vm", "network"})

            run_dirs = list((manifest.parent / ".gapctl" / "runs").iterdir())
            self.assertEqual(len(run_dirs), 2)
            for run_dir in run_dirs:
                self.assertTrue((run_dir / "run.json").is_file())
                self.assertTrue((run_dir / "junit.xml").is_file())
                self.assertTrue((run_dir / "isvctl.log").is_file())
            self.assertEqual(load_project(manifest).assessment.domains, ("vm", "network"))


if __name__ == "__main__":
    unittest.main()
