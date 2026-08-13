from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import jsonschema

from isv_readiness.context import (
    ContextError,
    _domain_audit_contract_rows,
    _selected_suite_entries,
    build_context_pack,
    context_cache_is_current,
    provider_contract_constraints,
    sync_context_sources,
)
from isv_readiness.project import (
    DEFAULT_INFERENCE_RA_URL,
    DEFAULT_NSRG_URL,
    build_bootstrap_plan,
    execute_bootstrap,
)
from isv_readiness.runs import latest_run, new_run_dir, write_run_record
from isv_readiness.scan.models import Evidence, GapReport, GapRow, Remediation
from isv_readiness.schema import load_schema

COMMIT = "b" * 40


class ContextTests(unittest.TestCase):
    def test_suite_contract_extraction_preserves_repeated_list_entries(self) -> None:
        validations = {
            "k8s_node_pools": [
                {
                    "K8sNodePoolCheck": {
                        "step": "create_test_node_pool",
                        "label_selector": "{{ steps.create_test_node_pool.label_selector }}",
                        "expected_replicas": "{{ steps.create_test_node_pool.expected_replicas }}",
                    }
                },
                {
                    "K8sNodePoolCheck": {
                        "step": "create_test_gpu_node_pool",
                        "label_selector": "{{ steps.create_test_gpu_node_pool.label_selector }}",
                        "expected_replicas": "{{ steps.create_test_gpu_node_pool.expected_replicas }}",
                    },
                    "UnrelatedCheck": {"step": "create_test_gpu_node_pool"},
                },
                {"K8sNodePoolCheck": {"step": "delete_test_node_pool", "expected_replicas": 0}},
            ],
            "kubernetes": {
                "checks": {"K8sNodeReadyCheck": {"require_all_ready": True}},
                "step": "setup",
            },
        }

        selected = _selected_suite_entries(
            validations,
            steps={"create_test_node_pool", "create_test_gpu_node_pool"},
            classes={"K8sNodePoolCheck"},
        )

        self.assertEqual(len(selected["k8s_node_pools"]), 2)
        self.assertEqual(
            selected["k8s_node_pools"][1],
            {
                "K8sNodePoolCheck": {
                    "step": "create_test_gpu_node_pool",
                    "label_selector": "{{ steps.create_test_gpu_node_pool.label_selector }}",
                    "expected_replicas": "{{ steps.create_test_gpu_node_pool.expected_replicas }}",
                }
            },
        )
        self.assertNotIn("kubernetes", selected)

    def test_semantic_audit_contract_includes_capability_consumers_and_lifecycle_edges(self) -> None:
        setup = _gap_report(Path("/tmp/provider")).rows[0]
        setup = GapRow(
            **{
                **setup.__dict__,
                "enrichment": {
                    "validation_phase": "setup",
                    "solution_profile": {"capability_id": "vm.default"},
                },
            }
        )
        consumer = GapRow(
            **{
                **setup.__dict__,
                "id": "gap_111111111111",
                "step_name": "exercise",
                "validation_class": "VmExerciseCheck",
                "enrichment": {
                    "validation_phase": "test",
                    "solution_profile": {"capability_id": "managed-vm"},
                },
            }
        )
        unrelated = GapRow(
            **{
                **consumer.__dict__,
                "id": "gap_222222222222",
                "validation_class": "UnrelatedCheck",
                "enrichment": {
                    "validation_phase": "test",
                    "solution_profile": {"capability_id": "other"},
                },
            }
        )
        audit = GapRow(
            **{
                **setup.__dict__,
                "id": "gap_333333333333",
                "validation_class": "DomainLifecycleAudit",
                "requirement_id": "managed-vm",
                "detection": "semantic",
                "enrichment": {"domain_audit": {"capability_id": "managed-vm"}},
            }
        )

        selected = _domain_audit_contract_rows((setup, consumer, unrelated, audit), audit)

        self.assertEqual([row.id for row in selected], [setup.id, consumer.id])

    def test_remediation_pack_preserves_large_api_spec_without_a_character_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            large_operation = "x" * 200_000
            (workspace / "openapi.yaml").write_text(
                "openapi: 3.0.3\npaths:\n  /vms:\n    post:\n      description: " + large_operation + "\n",
                encoding="utf-8",
            )
            cache = workspace / ".gapctl" / "cache"

            def fetcher(url: str, headers: dict[str, str]) -> bytes:
                del url, headers
                return b"reference context"

            sync_context_sources(project, manifest, cache, fetcher=fetcher)
            pack = build_context_pack(
                project,
                manifest,
                _gap_report(provider),
                gap_id="gap_0123456789ab",
                cache_dir=cache,
                environment={},
            ).to_dict()

            api_spec = next(item for item in pack["items"] if item["source_id"] == "primary_api_spec")
            self.assertIn(large_operation, api_spec["content"])
            self.assertFalse(api_spec["truncated"])
            self.assertIsNone(pack["budget"]["max_chars"])
            self.assertEqual(pack["budget"]["omitted_items"], 0)

    def test_normalizes_only_explicit_authoritative_lifecycle_timing(self) -> None:
        pack = {
            "items": [
                {
                    "kind": "api_spec",
                    "trust": "authoritative",
                    "content": ("runtime:\n  operation_timing:\n    lifecycle_step_timeout_seconds: 1200\n"),
                },
                {
                    "kind": "documentation",
                    "trust": "reference",
                    "content": "lifecycle_step_timeout_seconds: 30\n",
                },
            ]
        }

        self.assertEqual(
            provider_contract_constraints(pack),
            {"lifecycle_step_timeout_seconds": 1200.0},
        )

        pack["items"][0]["content"] = "runtime:\n  operation_timing:\n    lifecycle_step_timeout_seconds: short\n"
        with self.assertRaisesRegex(ContextError, "must be a number"):
            provider_contract_constraints(pack)

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
            self.assertEqual(by_id["inference_ra"].status, "error")
            self.assertEqual(by_id["primary_api_spec"].status, "synced")
            self.assertNotIn("validation_issues", by_id)
            self.assertNotIn("super-secret", json.dumps(by_id["primary_api_spec"].content))

    def test_context_pack_prioritizes_contracts_without_default_github_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            provider = project.provider_root(manifest)
            (provider / "scripts" / "vm").mkdir(parents=True)
            (provider / "scripts" / "common").mkdir()
            (provider / "config").mkdir()
            (provider / "scripts" / "vm" / "launch.py").write_text(
                "# TODO implement VM launch\n",
                encoding="utf-8",
            )
            (provider / "config" / "vm.yaml").write_text(
                "commands:\n"
                "  vm:\n"
                "    phases: [setup, test, teardown]\n"
                "    steps:\n"
                "      - name: launch\n"
                "        phase: setup\n"
                "        command: python ../scripts/vm/launch.py\n"
                "      - name: teardown\n"
                "        phase: teardown\n"
                "        command: python ../scripts/vm/teardown.py\n",
                encoding="utf-8",
            )
            (provider / "scripts" / "vm" / "teardown.py").write_text(
                "print({'success': True, 'resources_deleted': []})\n",
                encoding="utf-8",
            )
            (provider / "scripts" / "vm" / "client.py").write_text(
                "class DomainClient:\n    pass\n",
                encoding="utf-8",
            )
            (provider / "scripts" / "common" / "client.py").write_text(
                "class SharedClient:\n    pass\n",
                encoding="utf-8",
            )
            suite = workspace / "ai-cloud-validation" / "isvctl" / "configs" / "suites" / "vm.yaml"
            suite.parent.mkdir(parents=True)
            suite.write_text(
                "tests:\n"
                "  validations:\n"
                "    launch_contract:\n"
                "      step: launch\n"
                "      checks:\n"
                "        VmLaunchCheck:\n"
                "          expected_state: running\n",
                encoding="utf-8",
            )
            validations = (
                workspace
                / "ai-cloud-validation"
                / "isvtest"
                / "src"
                / "isvtest"
                / "validations"
            )
            validations.mkdir(parents=True)
            (validations / "vm.py").write_text(
                "def normalize_state(value):\n"
                "    return value.strip()\n"
                "\n"
                "class VmLaunchCheck:\n"
                "    def run(self):\n"
                "        checker = getattr(self, 'checker', None)\n"
                "        state = normalize_state(self.config['step_output'].get('state'))\n"
                "        return state == self.config.get('expected_state', 'running')\n",
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
            lifecycle = next(item for item in raw["items"] if item["source_id"] == "domain_lifecycle_contract")
            lifecycle_steps = {item["step_name"]: item for item in json.loads(lifecycle["content"])}
            self.assertTrue(lifecycle_steps["launch"]["selected"])
            provider_config = next(item for item in raw["items"] if item["source_id"] == "provider_config")
            self.assertIn("phase: teardown", provider_config["content"])
            lifecycle_script = next(
                item for item in raw["items"] if item["source_id"].startswith("provider_lifecycle_")
            )
            self.assertIn("resources_deleted", lifecycle_script["content"])
            shared_client = next(
                item for item in raw["items"] if item["source_id"].startswith("provider_shared_")
            )
            self.assertIn("SharedClient", shared_client["content"])
            domain_client = next(
                item for item in raw["items"] if item["source_id"].startswith("provider_domain_")
            )
            self.assertIn("DomainClient", domain_client["content"])
            upstream = next(item for item in raw["items"] if item["source_id"] == "upstream_target_contract")
            self.assertIn("expected_state", upstream["content"])
            self.assertIn("step_output", upstream["content"])
            upstream_payload = json.loads(upstream["content"])
            self.assertEqual(upstream_payload["validation_interface_projection"]["version"], "python_ast_v1")
            helper = upstream_payload["direct_dependency_sources"]["normalize_state"]
            self.assertIn("def normalize_state(value)", helper["source"])
            self.assertRegex(helper["sha256"], r"^[0-9a-f]{64}$")
            interface = upstream_payload["validation_interfaces"]["VmLaunchCheck"]
            self.assertFalse(interface["source"]["complete_source_in_pack"])
            self.assertEqual(interface["source"]["path"], "isvtest/src/isvtest/validations/vm.py")
            self.assertRegex(interface["source"]["class_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(interface["methods"][0]["signature"], "run(self)")
            self.assertIn("expected_state", {lookup["key"] for lookup in interface["data_lookups"]})
            self.assertIn("step_output", {lookup["key"] for lookup in interface["data_lookups"]})
            by_key = {lookup["key"]: lookup for lookup in interface["data_lookups"]}
            self.assertEqual(by_key["step_output"]["access"], "index")
            self.assertEqual(by_key["expected_state"]["access"], "get")
            self.assertTrue(by_key["expected_state"]["default_supplied"])
            self.assertEqual(
                interface["returns"][0]["expression"],
                "state == self.config.get('expected_state', 'running')",
            )
            self.assertEqual(interface["uncertainties"][0]["reason"], "dynamic call cannot be resolved statically")
            self.assertNotIn("def run", upstream["content"])
            self.assertEqual(raw["budget"]["omitted_items"], 0)
            self.assertTrue(all(not item["truncated"] for item in raw["items"]))
            self.assertTrue(
                any(
                    "deterministic projection of the pinned consumer source" in rule
                    for rule in raw["constraints"]
                )
            )
            self.assertFalse(
                any(
                    "Use only runtime environment names declared by the project" in rule
                    for rule in raw["constraints"]
                )
            )

            selected_config = json.loads(json.dumps(report["rows"][0]))
            selected_config["id"] = "gap_111111111111"
            selected_config["remediation"]["target"] = "config/vm.yaml"
            same_step = json.loads(json.dumps(selected_config))
            same_step["id"] = "gap_222222222222"
            same_step["validation_class"] = "VmLaunchSiblingCheck"
            other_step = json.loads(json.dumps(selected_config))
            other_step["id"] = "gap_333333333333"
            other_step["step_name"] = "delete"
            other_step["validation_class"] = "VmDeleteCheck"
            config_pack = build_context_pack(
                project,
                manifest,
                {"rows": [selected_config, same_step, other_step]},
                gap_id=selected_config["id"],
                cache_dir=cache,
                environment={},
            ).to_dict()
            config_related = next(
                item for item in config_pack["items"] if item["source_id"] == "related_target_gaps"
            )
            self.assertIn("VmLaunchSiblingCheck", config_related["content"])
            self.assertNotIn("VmDeleteCheck", config_related["content"])

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
            self.assertTrue(context_cache_is_current(project, cache, manifest_path=manifest))
            (workspace / "openapi.yaml").write_text(
                "paths:\n  /instances:\n    post: {}\n",
                encoding="utf-8",
            )
            self.assertFalse(context_cache_is_current(project, cache, manifest_path=manifest))
            sync_context_sources(project, manifest, cache, fetcher=fetcher_relevant)
            self.assertTrue(context_cache_is_current(project, cache, manifest_path=manifest))
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

    def test_inference_ra_is_preserved_whole_with_agent_readable_visuals(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir), existing_provider=True)
            (workspace / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
            cache = workspace / ".gapctl" / "context-cache"
            tail = "END-OF-INFERENCE-RA"
            source = (
                "# Inference RA\n\n"
                "```mermaid\n"
                "flowchart LR\n"
                '    client["Client"]\n'
                '    router["Dynamo Router"]\n'
                '    workers["GPU Workers<br />Prefill And Decode"]\n'
                '    evidence["Acceptance Evidence"]\n'
                "    client --> router --> workers\n"
                "    evidence -. gates .-> router\n"
                "```\n\n"
                + ("complete architecture context\n" * 700)
                + tail
            )

            def fetcher(url: str, headers: dict[str, str]) -> bytes:
                del headers
                if url == DEFAULT_NSRG_URL:
                    return _guide_index("introduction")
                if url == DEFAULT_INFERENCE_RA_URL:
                    return source.encode()
                return b"VM lifecycle reference guidance."

            sync_context_sources(project, manifest, cache, fetcher=fetcher)
            raw = json.loads((cache / "inference_ra.json").read_text(encoding="utf-8"))
            self.assertIn(tail, raw["content"])
            self.assertIn("```mermaid", raw["content"])
            self.assertIn("Agent-readable visual description:", raw["content"])
            self.assertIn("client means Client", raw["content"])
            self.assertIn("Client leads to Dynamo Router leads to GPU Workers", raw["content"])
            self.assertIn(
                "Acceptance Evidence has a dotted gates relationship to Dynamo Router",
                raw["content"],
            )


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
