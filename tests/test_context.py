from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.context import (
    build_context_pack,
    context_cache_is_current,
    sync_context_sources,
)
from isv_readiness.project import (
    DEFAULT_NSRG_URL,
    build_bootstrap_plan,
    execute_bootstrap,
)
from isv_readiness.runs import latest_run, new_run_dir, write_run_record
from isv_readiness.scan.models import Evidence, GapReport, GapRow, Remediation
from isv_readiness.schema import load_schema

COMMIT = "b" * 40


class ContextTests(unittest.TestCase):
    def test_sync_degrades_offline_and_redacts_local_api_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir))
            (workspace / "openapi.yaml").write_text(
                "paths:\n  /vms:\n    post: {}\nACME_TOKEN: super-secret\n",
                encoding="utf-8",
            )
            cache = workspace / ".gapctl" / "cache"

            def offline_fetcher(url: str, headers: dict[str, str]) -> bytes:
                del headers
                raise OSError(f"network unreachable: {url}")

            records = sync_context_sources(project, manifest, cache, fetcher=offline_fetcher)
            by_id = {record.source_id: record for record in records}

            self.assertEqual(by_id["nsrg"].status, "error")
            self.assertEqual(by_id["primary_api_spec"].status, "synced")
            self.assertNotIn("validation_issues", by_id)
            self.assertNotIn("super-secret", json.dumps(by_id["primary_api_spec"].content))

    def test_context_pack_prioritizes_contracts_without_default_github_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "config").mkdir()
            (provider / "scripts" / "vm" / "launch.py").write_text(
                "# TODO implement VM launch\n",
                encoding="utf-8",
            )
            (provider / "config" / "vm.yaml").write_text(
                "steps:\n  - name: launch\n    command: scripts/vm/launch.py\n",
                encoding="utf-8",
            )
            (workspace / "openapi.yaml").write_text(
                "paths:\n  /vms:\n    post:\n      operationId: launchVm\n",
                encoding="utf-8",
            )
            cache = workspace / ".gapctl" / "cache"

            def fetcher(url: str, headers: dict[str, str]) -> bytes:
                del headers
                if url == DEFAULT_NSRG_URL:
                    return _guide_index("introduction")
                return b"Infrastructure as a Service includes VM lifecycle operations."

            sync_context_sources(project, manifest, cache, fetcher=fetcher)
            report = _gap_report(provider).to_dict()
            profile_route = {
                "action": "implement_or_fix_adapter",
                "owned": True,
                "profile_status": "reviewed",
                "journey_stage": "validate",
            }
            report["rows"][0]["enrichment"]["solution_profile"] = profile_route
            related = json.loads(json.dumps(report["rows"][0]))
            related["id"] = "gap_abcdef012345"
            related["validation_class"] = "VmConnectivityCheck"
            report["rows"].append(related)

            pack = build_context_pack(
                project,
                manifest,
                report,
                gap_id="gap_0123456789ab",
                cache_dir=cache,
                environment={"ACME_TOKEN": "available-but-never-serialized"},
            )

            raw = pack.to_dict()
            schema = load_schema("context-pack.schema.json")
            jsonschema.validate(raw, schema)
            serialized = json.dumps(raw)
            self.assertIn("launchVm", serialized)
            self.assertNotIn("github.com/NVIDIA/ai-cloud-validation/issues", serialized)
            self.assertNotIn("available-but-never-serialized", serialized)
            self.assertEqual(raw["credentials"]["available_env"], ["ACME_TOKEN"])
            self.assertEqual(raw["items"][0]["trust"], "authoritative")
            runtime = next(item for item in raw["items"] if item["source_id"] == "provider_runtime_contract")
            self.assertIn("ACME_API_BASE", runtime["content"])
            self.assertIn("ACME_REGION", runtime["content"])
            related_item = next(item for item in raw["items"] if item["source_id"] == "related_target_gaps")
            self.assertIn("VmConnectivityCheck", related_item["content"])
            self.assertNotIn("aws_reference", related_item["content"])
            self.assertEqual(raw["budget"]["omitted_items"], 0)
            self.assertTrue(all(not item["truncated"] for item in raw["items"]))
            self.assertTrue(
                any(
                    "Use only runtime environment names declared by the project" in rule
                    for rule in raw["constraints"]
                )
            )

            with self.assertRaisesRegex(ValueError, "refusing to (?:omit|truncate) evidence"):
                build_context_pack(
                    project,
                    manifest,
                    report,
                    gap_id="gap_0123456789ab",
                    cache_dir=cache,
                    environment={},
                    max_chars=4_000,
                    feedback=("x" * 5_000,),
                )

    def test_latest_run_artifacts_enter_pack_as_top_ranked_empirical_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "config").mkdir()
            (provider / "scripts" / "vm" / "launch.py").write_text("# TODO\n", encoding="utf-8")
            (provider / "config" / "vm.yaml").write_text("steps: []\n", encoding="utf-8")
            (workspace / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
            report = _gap_report(provider)
            cache = workspace / ".gapctl" / "context-cache"
            runs_root = workspace / ".gapctl" / "runs"

            stale_id, stale_dir = new_run_dir(runs_root, "vm", created_at="20260101T000000Z")
            (stale_dir / "junit.xml").write_text(
                '<testsuite><testcase name="stale VmLaunchCheck launch"/></testsuite>',
                encoding="utf-8",
            )
            write_run_record(stale_dir, run_id=stale_id, domain="vm", config="config/vm.yaml", exit_code=1)
            fresh_id, fresh_dir = new_run_dir(runs_root, "vm", created_at="20260102T000000Z")
            (fresh_dir / "junit.xml").write_text(
                '<testsuite><testcase name="fresh VmLaunchCheck launch"><failure>'
                "VM launch returned no instance identifier\n"
                "ACME_TOKEN=leaked-value\n"
                "</failure></testcase></testsuite>",
                encoding="utf-8",
            )
            write_run_record(fresh_dir, run_id=fresh_id, domain="vm", config="config/vm.yaml", exit_code=1)

            resolved = latest_run(runs_root, "vm")
            assert resolved is not None
            self.assertEqual(resolved.run_id, fresh_id)
            self.assertIsNone(latest_run(runs_root, "network"))

            pack = build_context_pack(
                project, manifest, report, gap_id="gap_0123456789ab", cache_dir=cache, environment={}
            )
            raw = pack.to_dict()
            schema = load_schema("context-pack.schema.json")
            jsonschema.validate(raw, schema)
            self.assertEqual(raw["items"][0]["trust"], "empirical")
            self.assertEqual(raw["items"][0]["source_id"], "latest_run_vm_junit")
            runtime = next(item for item in raw["items"] if item["source_id"] == "provider_runtime_contract")
            self.assertEqual(json.loads(runtime["content"])["available_env_names"], [])
            serialized = json.dumps(raw)
            self.assertIn("fresh VmLaunchCheck", serialized)
            self.assertNotIn("stale VmLaunchCheck", serialized)
            self.assertNotIn("leaked-value", serialized)

    def test_ncp_guide_collects_every_indexed_page_and_zero_signal_guidance_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "config").mkdir()
            (provider / "scripts" / "vm" / "launch.py").write_text("# TODO\n", encoding="utf-8")
            (provider / "config" / "vm.yaml").write_text("steps: []\n", encoding="utf-8")
            (workspace / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
            report = _gap_report(provider)

            fetched: list[str] = []

            def fetcher_relevant(url: str, headers: dict[str, str]) -> bytes:
                del headers
                fetched.append(url)
                if url == DEFAULT_NSRG_URL:
                    return (
                        _guide_index("introduction")
                        + b"\n- [Part 1](https://docs.nvidia.com/dsx/ncp/part-1-software-reference-guide/"
                        b"ncp-software-reference-guide.md):"
                        + b"\n- [Part 2](https://docs.nvidia.com/dsx/ncp/part-2-software-components/"
                        b"nvidia-software-components.md):"
                        + b"\n- [Other product](https://docs.nvidia.com/dsx/ncp/inference-ra/home.md):"
                    )
                if url.endswith("introduction.md"):
                    return b"VM launch guidance: an instance must report its identifier."
                return b"Data center VM lifecycle guidance."

            cache = workspace / ".gapctl" / "cache-relevant"
            sync_context_sources(project, manifest, cache, fetcher=fetcher_relevant)
            nsrg = json.loads((cache / "nsrg.json").read_text(encoding="utf-8"))
            self.assertIn("VM launch guidance", nsrg["content"])
            self.assertIn("Data center VM lifecycle guidance", nsrg["content"])
            self.assertIn("Pages: 3", nsrg["content"])
            self.assertNotIn("inference-ra/home.md", fetched)
            self.assertTrue(context_cache_is_current(project, cache))
            pack = build_context_pack(
                project, manifest, report, gap_id="gap_0123456789ab", cache_dir=cache, environment={}
            )
            self.assertIn("nsrg", [item.source_id for item in pack.items])

            def fetcher_boilerplate(url: str, headers: dict[str, str]) -> bytes:
                del headers
                if url == DEFAULT_NSRG_URL:
                    return _guide_index("introduction")
                return b"Quantum basket weaving weekly."

            cache = workspace / ".gapctl" / "cache-irrelevant"
            sync_context_sources(project, manifest, cache, fetcher=fetcher_boilerplate)
            pack = build_context_pack(
                project, manifest, report, gap_id="gap_0123456789ab", cache_dir=cache, environment={}
            )
            self.assertNotIn("nsrg", [item.source_id for item in pack.items])


def _guide_index(*slugs: str) -> bytes:
    lines = [f"- [{slug}](https://docs.nvidia.com/dsx/ncp/software-reference-guide/{slug}.md):" for slug in slugs]
    return "\n".join(lines).encode()


def _project(root: Path, *, existing_provider: bool = False):
    workspace = root / "workspace"
    checkout = workspace / "ai-cloud-validation"
    (checkout / ".git").mkdir(parents=True)
    providers = checkout / "isvctl" / "configs" / "providers"
    (providers / "my-isv").mkdir(parents=True)
    if existing_provider:
        (providers / "acme").mkdir()
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
    project = execute_bootstrap(plan, runner=_git_runner)
    return workspace, project, plan.manifest_path


def _git_runner(command, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


def _gap_report(provider: Path) -> GapReport:
    return GapReport(
        schema_version="0.2.0",
        provider_repo=str(provider),
        domains=["vm"],
        rows=[
            GapRow(
                id="gap_0123456789ab",
                domain="vm",
                step_name="launch",
                validation_class="VmLaunchCheck",
                requirement_id="VM01",
                status="not_implemented",
                detection="static",
                stage="coverage",
                evidence=Evidence(
                    message="Provider launch script contains TODO",
                    script_path="scripts/vm/launch.py",
                    config_path="config/vm.yaml",
                ),
                remediation=Remediation(
                    auto_fixable=True,
                    target="scripts/vm/launch.py",
                    rerun_command="reviewed command",
                ),
                enrichment={"agent_action": "implement_or_fix_adapter"},
            )
        ],
    )


if __name__ == "__main__":
    unittest.main()
