from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml

from isv_readiness.live import (
    LiveRunError,
    _default_runner,
    _domain_config,
    _junit_validation_names,
    run_live_domain,
)
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap, load_project
from isv_readiness.schema import load_schema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMMIT = "c" * 40


class LiveRunTests(unittest.TestCase):
    def test_junit_coverage_ignores_injected_subtest_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            junit = Path(tempdir) / "junit.xml"
            junit.write_text(
                '<testsuite tests="3">'
                '<testcase name="test_validation[GpuCheck]" />'
                '<testcase name="test_validation[GpuCheck]::gpu-0" />'
                '<testcase name="StepSuccessCheck" />'
                "</testsuite>",
                encoding="utf-8",
            )

            self.assertEqual(
                _junit_validation_names(junit),
                ("GpuCheck", "StepSuccessCheck"),
            )

    def test_default_runner_uses_process_group_cleanup_boundary(self) -> None:
        expected = subprocess.CompletedProcess(["isvctl"], 0, "output", None)
        environment = {"PATH": "/bin"}

        with patch("isv_readiness.live.run_captured", return_value=expected) as captured:
            result = _default_runner(["isvctl"], Path("/tmp"), environment, 120)

        self.assertIs(result, expected)
        captured.assert_called_once_with(
            ["isvctl"],
            cwd=Path("/tmp"),
            input_text="",
            timeout_seconds=120,
            environment=environment,
            merge_stderr=True,
        )

    def test_kubernetes_prefers_config_inside_provider_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            provider = Path(tempdir) / "providers" / "acme"
            config = provider / "config" / "kubernetes.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("tests:\n  platform: kubernetes\n", encoding="utf-8")
            legacy = provider.with_suffix(".yaml")
            legacy.write_text("tests:\n  platform: legacy\n", encoding="utf-8")

            self.assertEqual(_domain_config(provider, "kubernetes"), config)
            config.unlink()
            self.assertEqual(_domain_config(provider, "kubernetes"), legacy)

    def test_live_run_requires_project_policy_and_explicit_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=False)
            with self.assertRaisesRegex(LiveRunError, "explicit --run-live"):
                run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=False,
                    commit_resolver=lambda root: COMMIT,
                )
            with self.assertRaisesRegex(LiveRunError, "policy disables"):
                run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=True,
                    commit_resolver=lambda root: COMMIT,
                )

    def test_targeted_live_run_uses_pinned_checkout_minimal_env_and_redacted_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=True)
            suite_path = manifest.parent / "ai-cloud-validation" / "isvctl" / "configs" / "suites" / "vm.yaml"
            suite = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
            suite["tests"]["validations"]["gpu_checks"] = {"checks": {"GpuCheck": {}}}
            suite_path.write_text(yaml.safe_dump(suite, sort_keys=False), encoding="utf-8")
            seen = {}

            def runner(command, cwd, environment, timeout):
                seen.update(command=list(command), cwd=cwd, environment=dict(environment), timeout=timeout)
                junit = Path(command[command.index("--junitxml") + 1])
                junit.write_text(
                    '<testsuite tests="1"><testcase name="test_vm[InstanceCreatedCheck]" /></testsuite>',
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "ACME_TOKEN=super-secret-value\nPASS\n", "")

            result = run_live_domain(
                project,
                manifest,
                domain="vm",
                artifacts_dir=Path(tempdir) / "artifacts",
                explicit_authorization=True,
                selection="InstanceCreatedCheck",
                runner=runner,
                commit_resolver=lambda root: COMMIT,
                environment={
                    "PATH": "/bin",
                    "HOME": "/home/test",
                    "ACME_TOKEN": "super-secret-value",
                    "ACME_REGION": "west",
                    "UNDECLARED_SECRET": "do-not-pass",
                },
            )

            self.assertTrue(result.success)
            self.assertEqual(result.selected_statuses, ("pass",))
            self.assertEqual(seen["command"][-3:], ["--", "-k", "InstanceCreatedCheck"])
            self.assertEqual(seen["environment"]["ACME_API_BASE"], "https://api.acme.invalid/v1")
            self.assertEqual(seen["environment"]["ACME_REGION"], "west")
            self.assertNotIn("UNDECLARED_SECRET", seen["environment"])
            self.assertNotIn("super-secret-value", Path(result.log_path).read_text(encoding="utf-8"))
            config_flags = [index for index, value in enumerate(seen["command"]) if value == "-f"]
            self.assertEqual(len(config_flags), 2)
            overlay = Path(seen["command"][config_flags[1] + 1])
            self.assertEqual(
                yaml.safe_load(overlay.read_text(encoding="utf-8")),
                {"tests": {"exclude": {"tests": ["GpuCheck"]}}},
            )
            schema = load_schema("live-run.schema.json")
            jsonschema.validate(result.to_dict(), schema)

    def test_skipped_rows_do_not_veto_success_but_all_skipped_is_not_success(self) -> None:
        """A skipped row is a declared config exclusion, not a failure (exit 0 runs green)."""
        cases = [
            # (junit body, expected success)
            (
                '<testsuite tests="5">'
                '<testcase name="test_vm[InstanceCreatedCheck]" />'
                '<testcase name="test_vm[InstanceListCheck]" />'
                '<testcase name="test_vm[InstanceStateCheck]" />'
                '<testcase name="test_vm[StepSuccessCheck]" />'
                '<testcase name="test_vm[GpuCheck]"><skipped message="excluded by label: ssh" /></testcase>'
                "</testsuite>",
                True,
            ),
            (
                '<testsuite tests="1">'
                '<testcase name="test_vm[GpuCheck]"><skipped message="excluded by label: ssh" /></testcase>'
                "</testsuite>",
                False,
            ),
            (
                '<testsuite tests="2">'
                '<testcase name="test_vm[GpuCheck]" />'
                '<testcase name="test_vm[InstanceCreatedCheck]"><skipped message="runtime prerequisite" /></testcase>'
                "</testsuite>",
                False,
            ),
        ]
        for junit_body, expected in cases:
            with tempfile.TemporaryDirectory() as tempdir:
                project, manifest = _project(Path(tempdir), allow_live=True)

                def runner(command, cwd, environment, timeout, body=junit_body):
                    junit = Path(command[command.index("--junitxml") + 1])
                    junit.write_text(body, encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, "PASS\n", "")

                result = run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=True,
                    runner=runner,
                    commit_resolver=lambda root: COMMIT,
                    environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "x", "ACME_REGION": "west"},
                )
                self.assertEqual(result.success, expected, f"junit={junit_body[:60]}")

    def test_full_domain_run_fails_closed_when_junit_omits_expected_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=True)

            def runner(command, cwd, environment, timeout):
                del cwd, environment, timeout
                junit = Path(command[command.index("--junitxml") + 1])
                junit.write_text(
                    '<testsuite tests="1"><testcase name="test_vm[InstanceCreatedCheck]" /></testsuite>',
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "PASS\n", "")

            result = run_live_domain(
                project,
                manifest,
                domain="vm",
                artifacts_dir=Path(tempdir) / "artifacts",
                explicit_authorization=True,
                runner=runner,
                commit_resolver=lambda root: COMMIT,
                environment={"PATH": "/bin", "HOME": "/home/test", "ACME_TOKEN": "x", "ACME_REGION": "west"},
            )

            missing = [
                row
                for row in result.report["rows"]
                if row.get("enrichment", {}).get("contract_error") == "missing_junit_result"
            ]
            self.assertFalse(result.success)
            self.assertEqual(
                {row["validation_class"] for row in missing},
                {"InstanceListCheck", "InstanceStateCheck", "StepSuccessCheck"},
            )
            self.assertIn("error", result.selected_statuses)

    def test_live_run_rejects_checkout_drift_and_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            project, manifest = _project(Path(tempdir), allow_live=True)
            with self.assertRaisesRegex(LiveRunError, "drifted"):
                run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=True,
                    commit_resolver=lambda root: "d" * 40,
                )
            with self.assertRaisesRegex(LiveRunError, "ACME_TOKEN"):
                run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=True,
                    commit_resolver=lambda root: COMMIT,
                    environment={"PATH": "/bin"},
                )

            with self.assertRaisesRegex(LiveRunError, "ACME_REGION"):
                run_live_domain(
                    project,
                    manifest,
                    domain="vm",
                    artifacts_dir=Path(tempdir) / "artifacts",
                    explicit_authorization=True,
                    commit_resolver=lambda root: COMMIT,
                    environment={"PATH": "/bin", "ACME_TOKEN": "set"},
                )


def _project(root: Path, *, allow_live: bool):
    workspace = root / "workspace"
    checkout = workspace / "ai-cloud-validation"
    shutil.copytree(FIXTURES / "ai-cloud-validation", checkout)
    (checkout / ".git").mkdir()
    (checkout / "isvctl" / "configs" / "providers" / "my-isv").mkdir()
    provider = workspace / "provider"
    shutil.copytree(FIXTURES / "provider_repo", provider)
    plan = build_bootstrap_plan(
        workspace,
        provider_name="acme",
        domains=["vm"],
        validation_root=checkout,
        api_base_url="https://api.acme.invalid/v1",
        api_base_url_env="ACME_API_BASE",
        api_spec="openapi.yaml",
        auth_env=["ACME_TOKEN"],
        pass_env=["ACME_REGION"],
    )
    execute_bootstrap(plan, runner=_git_runner)
    _ratify_profile(workspace / "solution-profile.yaml")
    raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
    raw["provider"]["path"] = "provider"
    raw["provider"]["state"] = "existing"
    raw["execution"]["allow_live_runs"] = allow_live
    raw["execution"]["run_environment"] = "staging"
    plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_project(plan.manifest_path), plan.manifest_path


def _ratify_profile(path: Path) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["solution"]["profile_status"] = "reviewed"
    raw["journey"] = {"stage": "validate", "status": "ready"}
    raw["domains"][0]["capabilities"] = [
        {
            "id": "gpu-excluded",
            "name": "GPU validation excluded in this fixture",
            "selectors": {"validation_classes": ["GpuCheck"]},
            "coverage": "out_of_scope",
            "validation_mode": "skip",
            "rationale": "The fixture explicitly excludes this optional check.",
        }
    ]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _git_runner(command, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


if __name__ == "__main__":
    unittest.main()
