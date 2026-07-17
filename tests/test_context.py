from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.context import (
    build_context_pack,
    sync_context_sources,
)
from isv_readiness.project import build_bootstrap_plan, execute_bootstrap
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

            self.assertEqual(by_id["validation_issues"].status, "missing")
            self.assertEqual(by_id["nsrg"].status, "missing")
            self.assertEqual(by_id["primary_api_spec"].status, "synced")
            self.assertNotIn("super-secret", json.dumps(by_id["primary_api_spec"].content))

    def test_github_issue_sync_filters_pull_requests_and_never_caches_token(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir))
            (workspace / "openapi.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
            seen: list[tuple[str, dict[str, str]]] = []

            def fetcher(url: str, headers: dict[str, str]) -> bytes:
                seen.append((url, dict(headers)))
                if "api.github.com" in url:
                    return json.dumps(
                        [
                            {
                                "number": 12,
                                "title": "VM launch validation contract",
                                "body": "Implement VmLaunchCheck for the provider adapter",
                                "labels": [{"name": "vm"}],
                                "html_url": "https://github.com/NVIDIA/ai-cloud-validation/issues/12",
                                "updated_at": "2026-06-01T00:00:00Z",
                            },
                            {"number": 99, "title": "PR", "pull_request": {}},
                        ]
                    ).encode()
                return b"VM lifecycle guidance"

            records = sync_context_sources(
                project,
                manifest,
                workspace / ".gapctl" / "cache",
                fetcher=fetcher,
                environment={"GITHUB_TOKEN": "do-not-cache"},
            )
            issues = next(record for record in records if record.source_id == "validation_issues")

            self.assertEqual(len(issues.content), 1)
            self.assertEqual(issues.content[0]["number"], 12)
            self.assertEqual(seen[0][1]["Authorization"], "Bearer do-not-cache")
            cache_text = "".join(
                path.read_text(encoding="utf-8")
                for path in (workspace / ".gapctl" / "cache").glob("*.json")
            )
            self.assertNotIn("do-not-cache", cache_text)

    def test_context_pack_prioritizes_contracts_and_relevant_advisory_evidence(self) -> None:
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
                if "api.github.com" in url:
                    return json.dumps(
                        [
                            {
                                "number": 12,
                                "title": "VmLaunchCheck needs provider script",
                                "body": "VM launch should return an instance identifier",
                                "labels": [{"name": "vm"}],
                                "html_url": "https://github.com/NVIDIA/ai-cloud-validation/issues/12",
                            },
                            {
                                "number": 13,
                                "title": "Unrelated Kubernetes telemetry",
                                "body": "GPU metrics",
                                "labels": [{"name": "kubernetes"}],
                                "html_url": "https://github.com/NVIDIA/ai-cloud-validation/issues/13",
                            },
                        ]
                    ).encode()
                return b"Infrastructure as a Service includes VM lifecycle operations."

            sync_context_sources(project, manifest, cache, fetcher=fetcher)
            report = _gap_report(provider)

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
            self.assertIn("issues/12", serialized)
            self.assertNotIn("issues/13", serialized)
            self.assertNotIn("available-but-never-serialized", serialized)
            self.assertEqual(raw["credentials"]["available_env"], ["ACME_TOKEN"])
            self.assertEqual(raw["items"][0]["trust"], "authoritative")

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
            serialized = json.dumps(raw)
            self.assertIn("fresh VmLaunchCheck", serialized)
            self.assertNotIn("stale VmLaunchCheck", serialized)
            self.assertNotIn("leaked-value", serialized)

    def test_html_sources_are_extracted_to_prose_and_zero_signal_guidance_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "config").mkdir()
            (provider / "scripts" / "vm" / "launch.py").write_text("# TODO\n", encoding="utf-8")
            (provider / "config" / "vm.yaml").write_text("steps: []\n", encoding="utf-8")
            (workspace / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
            report = _gap_report(provider)

            def fetcher_relevant(url: str, headers: dict[str, str]) -> bytes:
                del headers
                if "api.github.com" in url:
                    return b"[]"
                return (
                    b"<!DOCTYPE html><html><head><style>.x{color:red}</style>"
                    b"<script>let a=1;</script></head><body>"
                    b"<p>VM launch guidance: an instance must report its identifier.</p>"
                    b"</body></html>"
                )

            cache = workspace / ".gapctl" / "cache-relevant"
            sync_context_sources(project, manifest, cache, fetcher=fetcher_relevant)
            nsrg = json.loads((cache / "nsrg.json").read_text(encoding="utf-8"))
            self.assertNotIn("<", nsrg["content"])
            self.assertNotIn("color:red", nsrg["content"])
            self.assertIn("VM launch guidance", nsrg["content"])
            pack = build_context_pack(
                project, manifest, report, gap_id="gap_0123456789ab", cache_dir=cache, environment={}
            )
            self.assertIn("nsrg", [item.source_id for item in pack.items])

            def fetcher_boilerplate(url: str, headers: dict[str, str]) -> bytes:
                del headers
                if "api.github.com" in url:
                    return b"[]"
                return b"<!DOCTYPE html><html><body><p>Quantum basket weaving weekly.</p></body></html>"

            cache = workspace / ".gapctl" / "cache-irrelevant"
            sync_context_sources(project, manifest, cache, fetcher=fetcher_boilerplate)
            pack = build_context_pack(
                project, manifest, report, gap_id="gap_0123456789ab", cache_dir=cache, environment={}
            )
            self.assertNotIn("nsrg", [item.source_id for item in pack.items])


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
        api_spec="openapi.yaml",
        auth_env=["ACME_TOKEN"],
    )
    project = execute_bootstrap(plan, runner=_git_runner)
    return workspace, project, plan.manifest_path


def _git_runner(command, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")


def _gap_report(provider: Path) -> GapReport:
    return GapReport(
        schema_version="0.1.0",
        provider_repo=str(provider),
        domains=["vm"],
        rows=[
            GapRow(
                id="gap_0123456789ab",
                domain="vm",
                step_name="launch",
                validation_class="VmLaunchCheck",
                requirement_id="VM01",
                milestone="M1",
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
