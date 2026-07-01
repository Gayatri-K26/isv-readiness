from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from isv_readiness.live import LiveRunError, run_live_domain
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap, load_project

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMMIT = "c" * 40


class LiveRunTests(unittest.TestCase):
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
            self.assertEqual(result.report["isv_context"]["run_env"], "staging")
            schema = json.loads((ROOT / "schemas" / "live-run.schema.json").read_text(encoding="utf-8"))
            jsonschema.validate(result.to_dict(), schema)

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
    raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
    raw["provider"]["path"] = "provider"
    raw["provider"]["state"] = "existing"
    raw["execution"]["allow_live_runs"] = allow_live
    raw["execution"]["run_environment"] = "staging"
    plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_project(plan.manifest_path), plan.manifest_path


def _git_runner(command, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


if __name__ == "__main__":
    unittest.main()
