from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from isv_readiness.live import LiveRunResult
from isv_readiness.project import ProjectError, load_project
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.simple import cmd_init, cmd_status, cmd_test
from tests.test_live import _project


class SimpleCommandTests(unittest.TestCase):
    def test_init_passes_requested_validation_ref_to_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            def stop_before_clone(plan, *, overwrite):
                self.assertEqual(plan.validation_ref, "release-1.2")
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
                        api_url=None,
                        auth_envs=[],
                        api_spec=None,
                        validation_ref="release-1.2",
                    ),
                    2,
                )

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

            report = json.loads((manifest.parent / "gaps.json").read_text(encoding="utf-8"))
            self.assertEqual(report["domains"], ["vm", "network"])
            self.assertTrue(any(row["domain"] == "network" for row in report["rows"]))
            self.assertTrue(any(row["detection"] == "dynamic" for row in report["rows"]))

            run_dirs = list((manifest.parent / ".gapctl" / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            self.assertTrue((run_dirs[0] / "run.json").is_file())
            self.assertTrue((run_dirs[0] / "junit.xml").is_file())
            self.assertTrue((run_dirs[0] / "isvctl.log").is_file())
            self.assertEqual(load_project(manifest).assessment.domains, ("vm", "network"))


if __name__ == "__main__":
    unittest.main()
