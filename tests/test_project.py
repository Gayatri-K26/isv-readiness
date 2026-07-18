from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from isv_readiness.project import (
    ProjectError,
    build_bootstrap_plan,
    execute_bootstrap,
    load_project,
)
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import load_solution_profile

COMMIT = "a" * 40


class ProjectBootstrapTests(unittest.TestCase):
    def test_building_bootstrap_plan_does_not_create_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            plan = build_bootstrap_plan(workspace, provider_name="acme", domains=["vm", "k8s"])

            self.assertFalse(workspace.exists())
            self.assertTrue(plan.clone_required)
            self.assertEqual(plan.domains, ("vm", "kubernetes"))

    def test_existing_checkout_writes_pinned_scoped_project(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            checkout = workspace / "ai-cloud-validation"
            _make_checkout(checkout, provider="acme")
            plan = build_bootstrap_plan(
                workspace,
                provider_name="acme",
                domains=["vm", "k8s", "vm"],
                validation_root=checkout,
                api_base_url="https://api.acme.invalid/v1",
                api_spec="docs/openapi.yaml",
                auth_env=["ACME_TOKEN"],
            )

            project = execute_bootstrap(plan, runner=_git_runner)

            self.assertEqual(project.validation.resolved_commit, COMMIT)
            self.assertEqual(project.assessment.domains, ("vm", "kubernetes"))
            self.assertEqual(project.provider.state, "existing")
            self.assertFalse(project.execution.allow_live_runs)
            self.assertEqual(project.apis[0].auth_env, ("ACME_TOKEN",))
            profile = load_solution_profile(project.resolve_path(plan.manifest_path, project.assessment.profile))
            self.assertEqual(profile.resolve("vm").action, "implement_or_fix_adapter")
            self.assertEqual(load_project(plan.manifest_path), project)
            raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
            schema = load_schema("project.schema.json")
            jsonschema.validate(raw, schema)

    def test_missing_checkout_is_cloned_before_manifest_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            commands: list[tuple[str, ...]] = []

            def runner(command: list[str] | tuple[str, ...], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
                del cwd, timeout
                command = tuple(command)
                commands.append(command)
                if command[:2] == ("git", "clone"):
                    _make_checkout(Path(command[-1]))
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")

            plan = build_bootstrap_plan(workspace, provider_name="new-isv", domains=["network"])
            project = execute_bootstrap(plan, runner=runner)

            self.assertTrue(commands[0][:4] == ("git", "clone", "--branch", "main"))
            self.assertEqual(project.provider.state, "new")
            self.assertTrue(plan.manifest_path.exists())

    def test_rejects_secret_values_as_credential_names_and_bad_checkouts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            with self.assertRaisesRegex(ProjectError, "environment variable names"):
                build_bootstrap_plan(
                    workspace,
                    provider_name="acme",
                    domains=["vm"],
                    auth_env=["ACME_TOKEN=secret"],
                )

            empty = workspace / "empty"
            empty.mkdir()
            plan = build_bootstrap_plan(
                workspace / "project",
                provider_name="acme",
                domains=["vm"],
                validation_root=empty,
            )
            with self.assertRaisesRegex(ProjectError, "Not an ai-cloud-validation checkout"):
                execute_bootstrap(plan, runner=_git_runner)

    def test_supplied_profile_must_cover_every_owned_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            checkout = root / "ai-cloud-validation"
            _make_checkout(checkout)
            # Qualify phase for a single owned domain produces a draft profile.
            single = build_bootstrap_plan(
                root / "single",
                provider_name="acme",
                domains=["vm"],
                validation_root=checkout,
            )
            execute_bootstrap(single, runner=_git_runner)
            draft_profile = single.manifest_path.parent / "solution-profile.yaml"

            # Reusing that vm-only profile for an ISV that also owns network must fail:
            # the profile does not cover every owned domain.
            multi = build_bootstrap_plan(
                root / "multi",
                provider_name="acme",
                domains=["vm", "network"],
                validation_root=checkout,
                profile=draft_profile,
            )
            with self.assertRaisesRegex(ProjectError, "does not cover owned domains"):
                execute_bootstrap(multi, runner=_git_runner)


def _make_checkout(root: Path, provider: str | None = None) -> None:
    (root / ".git").mkdir(parents=True)
    providers = root / "isvctl" / "configs" / "providers"
    (providers / "my-isv").mkdir(parents=True)
    if provider:
        (providers / provider).mkdir()


def _git_runner(
    command: list[str] | tuple[str, ...], cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


if __name__ == "__main__":
    unittest.main()
