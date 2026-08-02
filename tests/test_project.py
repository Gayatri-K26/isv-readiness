from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema
import yaml

from isv_readiness.project import (
    DEFAULT_INFERENCE_RA_URL,
    DEFAULT_NSRG_URL,
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

    def test_storage_is_a_supported_project_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            checkout = workspace / "ai-cloud-validation"
            _make_checkout(checkout)
            plan = build_bootstrap_plan(
                workspace,
                provider_name="acme",
                domains=["storage"],
                validation_root=checkout,
            )

            project = execute_bootstrap(plan, runner=_git_runner)

            self.assertEqual(project.assessment.domains, ("storage",))
            profile = load_solution_profile(project.resolve_path(plan.manifest_path, project.assessment.profile))
            self.assertEqual(profile.domains[0].domain, "storage")

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
            self.assertEqual(project.execution.max_failure_groups, 10)
            self.assertEqual(project.apis[0].auth_env, ("ACME_TOKEN",))
            self.assertEqual(
                [source.id for source in project.context_sources],
                ["nsrg", "inference_ra", "primary_api_spec"],
            )
            self.assertEqual(project.context_sources[0].location, DEFAULT_NSRG_URL)
            self.assertTrue(project.context_sources[0].required)
            self.assertEqual(project.context_sources[1].location, DEFAULT_INFERENCE_RA_URL)
            self.assertEqual(project.context_sources[1].trust, "reference")
            self.assertTrue(project.context_sources[1].required)
            profile = load_solution_profile(project.resolve_path(plan.manifest_path, project.assessment.profile))
            self.assertEqual(profile.resolve("vm").action, "implement_or_fix_adapter")
            self.assertEqual(load_project(plan.manifest_path), project)
            raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
            schema = load_schema("project.schema.json")
            jsonschema.validate(raw, schema)

    def test_bootstrap_accepts_non_api_isv_context(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            workspace = root / "workspace"
            checkout = workspace / "ai-cloud-validation"
            _make_checkout(checkout)
            architecture = root / "reference-architecture.md"
            architecture.write_text("# Acme reference architecture\n", encoding="utf-8")
            document_tree = root / "product-docs"
            document_tree.mkdir()
            (document_tree / "operations.md").write_text("# Operations\n", encoding="utf-8")

            plan = build_bootstrap_plan(
                workspace,
                provider_name="acme",
                domains=["vm"],
                validation_root=checkout,
                context_inputs=[
                    str(architecture),
                    str(document_tree),
                    "https://docs.acme.invalid/platform",
                    str(architecture),
                ],
            )
            project = execute_bootstrap(plan, runner=_git_runner)

            self.assertEqual(project.apis, ())
            supplied = [source for source in project.context_sources if "isv_supplied" in source.labels]
            self.assertEqual(
                [(source.id, source.kind) for source in supplied],
                [
                    ("isv_context_1", "local_file"),
                    ("isv_context_2", "local_tree"),
                    ("isv_context_3", "web_url"),
                ],
            )
            self.assertTrue(all(source.required and source.trust == "authoritative" for source in supplied))

    def test_bootstrap_rejects_a_missing_context_input_before_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            with self.assertRaisesRegex(ProjectError, "Context input not found"):
                build_bootstrap_plan(
                    workspace,
                    provider_name="acme",
                    domains=["vm"],
                    context_inputs=[str(Path(tempdir) / "missing.md")],
                )
            self.assertFalse(workspace.exists())

    def test_missing_checkout_is_cloned_before_manifest_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            commands: list[tuple[str, ...]] = []

            def runner(
                command: list[str] | tuple[str, ...], cwd: Path, timeout: int
            ) -> subprocess.CompletedProcess[str]:
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

    def test_loading_legacy_project_adds_standard_inference_reference_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            checkout = workspace / "ai-cloud-validation"
            _make_checkout(checkout, provider="acme")
            plan = build_bootstrap_plan(
                workspace,
                provider_name="acme",
                domains=["vm"],
                validation_root=checkout,
            )
            execute_bootstrap(plan, runner=_git_runner)
            raw = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
            raw["execution"].pop("max_failure_groups")
            raw["context_sources"] = [
                source for source in raw["context_sources"] if source["id"] != "inference_ra"
            ]
            plan.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            loaded = load_project(plan.manifest_path)

            self.assertEqual(loaded.execution.max_failure_groups, 10)
            inference = next(source for source in loaded.context_sources if source.id == "inference_ra")
            self.assertEqual(inference.location, DEFAULT_INFERENCE_RA_URL)
            self.assertEqual(inference.domains, ("vm",))
            persisted = yaml.safe_load(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn("inference_ra", [source["id"] for source in persisted["context_sources"]])

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


def _git_runner(command: list[str] | tuple[str, ...], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


if __name__ == "__main__":
    unittest.main()
