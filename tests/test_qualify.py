from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from isv_readiness.context import (
    QUALIFICATION_MAPPING_RULES,
    build_qualify_pack,
    sync_context_sources,
)
from isv_readiness.project import (
    DEFAULT_INFERENCE_RA_URL,
    DEFAULT_NSRG_URL,
    build_bootstrap_plan,
    execute_bootstrap,
)
from isv_readiness.qualify import (
    QualifyError,
    build_qualify_catalog,
    effective_check_summary,
    empirical_conflicts,
    profile_draft_diff,
    run_profile_draft,
)
from isv_readiness.runs import new_run_dir, write_run_record
from isv_readiness.solution_profile import parse_solution_profile
from isv_readiness.validation_adapter import IsvctlAdapter

COMMIT = "c" * 40

CATALOG_PAYLOAD = {
    "isvTestVersion": "0.8.0",
    "entries": [
        {
            "name": "VmLaunchCheck",
            "description": "Check a VM launches and reports its identifier",
            "labels": ["vm", "min_req"],
            "module": "isvtest.validations.vm",
            "platforms": ["VM"],
        }
    ],
}

MERGED_CONFIG = {
    "version": "1.0",
    "commands": {},
    "tests": {
        "platform": "vm",
        "validations": {
            "vm": {
                "checks": {
                    "VmLaunchCheck": {"test_id": "VM01-01", "step": "launch_instance"},
                    "VmLaunchCheck-terminate": {"test_id": "VM01-02", "step": "terminate_instance"},
                }
            }
        },
    },
}


def _isvctl_runner(command: Sequence[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess[str]:
    del cwd, timeout
    if "--version" in command:
        return subprocess.CompletedProcess(command, 0, "isvctl 0.8.0\n", "")
    if "catalog" in command:
        return subprocess.CompletedProcess(command, 0, json.dumps(CATALOG_PAYLOAD), "")
    return subprocess.CompletedProcess(command, 0, json.dumps(MERGED_CONFIG), "")


def _profile_payload(domains: Sequence[str] = ("vm",), coverage: str = "covered") -> dict:
    return {
        "schema_version": "0.1.0",
        "solution": {
            "id": "acme",
            "name": "acme",
            "vendor": "acme",
            "version": "1.0",
            "profile_status": "confirmed",
            "target_environment": "lab",
        },
        "journey": {"stage": "validate", "status": "ready"},
        "actors": [{"id": "isv", "name": "acme", "kind": "isv"}],
        "components": [
            {
                "id": "provider",
                "name": "acme",
                "version": "1.0",
                "kind": "product",
                "supplier_actor_id": "isv",
                "depends_on": [],
                "source_refs": [],
            }
        ],
        "domains": [
            {
                "domain": domain,
                "name": domain,
                "owned": True,
                "coverage": coverage,
                "validation_mode": "test",
                "capability_owner_actor_id": "isv",
                "provider_adapter_owner_actor_id": "isv",
                "component_ids": ["provider"],
                "rationale": "the packed API spec exposes this capability",
            }
            for domain in domains
        ],
    }


def _draft_runner(payload: Mapping) -> object:
    def runner(
        command: Sequence[str], cwd: Path, request: str, env: Mapping[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, timeout
        json.loads(request)  # request must be one JSON document
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    return runner


class QualifyCatalogTests(unittest.TestCase):
    def test_catalog_distills_checks_and_canonicalizes_domains(self) -> None:
        adapter = IsvctlAdapter(Path("/tmp"), runner=_isvctl_runner)
        catalog = build_qualify_catalog(adapter, ["vm", "k8s", "storage"])

        self.assertEqual(sorted(catalog["domains"]), ["kubernetes", "storage", "vm"])
        vm = catalog["domains"]["vm"]
        self.assertEqual(vm["suite"], "isvctl/configs/suites/vm.yaml")
        self.assertEqual(vm["steps"], ["launch_instance", "terminate_instance"])
        by_name = {check["name"]: check for check in vm["checks"]}
        self.assertEqual(by_name["VmLaunchCheck"]["test_id"], "VM01-01")
        self.assertEqual(by_name["VmLaunchCheck"]["category"], "vm")
        self.assertEqual(by_name["VmLaunchCheck-terminate"]["check"], "VmLaunchCheck")
        self.assertEqual(by_name["VmLaunchCheck"]["description"], CATALOG_PAYLOAD["entries"][0]["description"])
        self.assertEqual(
            catalog["domains"]["storage"]["suite"],
            "isvctl/configs/suites/storage.yaml",
        )

    def test_catalog_rejects_unknown_domain(self) -> None:
        adapter = IsvctlAdapter(Path("/tmp"), runner=_isvctl_runner)
        with self.assertRaises(QualifyError):
            build_qualify_catalog(adapter, ["metal_as_a_vibe"])


class QualifyPackTests(unittest.TestCase):
    def test_pack_orders_empirical_before_contracts_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir))
            (workspace / "openapi.yaml").write_text(
                "paths:\n  /vms:\n    post:\n      operationId: launchVm\n", encoding="utf-8"
            )
            cache = workspace / ".gapctl" / "context-cache"
            sync_context_sources(project, manifest, cache, fetcher=_offline_fetcher)
            run_id, run_dir = new_run_dir(workspace / ".gapctl" / "runs", "vm", created_at="20260101T000000Z")
            (run_dir / "junit.xml").write_text(
                '<testsuite><testcase name="VmLaunchCheck launch instance"><failure>no id</failure></testcase></testsuite>',
                encoding="utf-8",
            )
            write_run_record(run_dir, run_id=run_id, domain="vm", config="config/vm.yaml", exit_code=1)

            adapter = IsvctlAdapter(Path("/tmp"), runner=_isvctl_runner)
            catalog = build_qualify_catalog(adapter, project.assessment.domains)
            pack = build_qualify_pack(project, catalog, cache_dir=cache)

            self.assertEqual(pack["purpose"], "qualify_draft")
            self.assertEqual(pack["project"]["declared_domains"], ["vm"])
            source_ids = [item["source_id"] for item in pack["items"]]
            self.assertEqual(source_ids[0], "latest_run_vm_junit")
            self.assertIn("catalog_vm", source_ids)
            self.assertIn("primary_api_spec", source_ids)
            for rule in QUALIFICATION_MAPPING_RULES:
                self.assertIn(rule, pack["constraints"])
            serialized = json.dumps(pack)
            self.assertIn("VM01-01", serialized)
            self.assertIn("launchVm", serialized)


class AuthoritativeWholeTests(unittest.TestCase):
    def test_oversized_api_spec_enters_pack_whole_not_excerpted(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir))
            # >12k spec whose tail shares no vocabulary with the catalog —
            # exactly what block-relevance excerpting would have cut.
            filler = "\n\n".join(f"unrelated billing paragraph {i} zebra quartz" for i in range(400))
            spec = "paths:\n  /vms:\n    post:\n      operationId: launchVm\n\n" + filler
            self.assertGreater(len(spec), 12_000)
            (workspace / "openapi.yaml").write_text(spec, encoding="utf-8")
            cache = workspace / ".gapctl" / "context-cache"
            sync_context_sources(project, manifest, cache, fetcher=_offline_fetcher)

            adapter = IsvctlAdapter(Path("/tmp"), runner=_isvctl_runner)
            catalog = build_qualify_catalog(adapter, project.assessment.domains)
            pack = build_qualify_pack(project, catalog, cache_dir=cache)

            item = next(item for item in pack["items"] if item["source_id"] == "primary_api_spec")
            self.assertFalse(item["truncated"])
            self.assertIn("unrelated billing paragraph 399", item["content"])

    def test_qualify_keeps_complete_references_beyond_the_old_character_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace, project, manifest = _project(Path(tempdir))
            (workspace / "openapi.yaml").write_text("paths: {}\n", encoding="utf-8")
            cache = workspace / ".gapctl" / "context-cache"
            guide_tail = "END-OF-GUIDE"

            def fetcher(url: str, headers: Mapping[str, str]) -> bytes:
                del headers
                if url == DEFAULT_NSRG_URL:
                    return (
                        b"- [Introduction](https://docs.nvidia.com/dsx/ncp/software-reference-guide/introduction.md):"
                    )
                return ("VM lifecycle reference\n" + "x" * 310_000 + guide_tail).encode()

            sync_context_sources(project, manifest, cache, fetcher=fetcher)
            catalog = build_qualify_catalog(IsvctlAdapter(Path("/tmp"), runner=_isvctl_runner), ["vm"])
            pack = build_qualify_pack(project, catalog, cache_dir=cache)

            item = next(item for item in pack["items"] if item["source_id"] == "nsrg")
            self.assertFalse(item["truncated"])
            self.assertIn(guide_tail, item["content"])
            inference = next(item for item in pack["items"] if item["source_id"] == "inference_ra")
            self.assertFalse(inference["truncated"])
            self.assertIn(guide_tail, inference["content"])
            self.assertIsNone(pack["budget"]["max_chars"])
            self.assertGreater(pack["budget"]["used_chars"], 300_000)
            self.assertEqual(pack["budget"]["omitted_items"], 0)


class ProfileDraftTests(unittest.TestCase):
    def test_draft_is_hardened_to_qualify_stage_draft_status(self) -> None:
        pack = {"project": {"declared_domains": ["vm"]}}
        raw = run_profile_draft(
            pack,
            command=["true"],
            cwd=Path("/tmp"),
            runner=_draft_runner(_profile_payload()),
        )
        self.assertEqual(raw["solution"]["profile_status"], "draft")
        self.assertEqual(raw["journey"], {"stage": "qualify", "status": "in_progress"})

    def test_cited_pack_items_are_declared_as_sources(self) -> None:
        pack = {
            "project": {"declared_domains": ["vm"]},
            "items": [{"source_id": "catalog_vm", "origin": "gapctl://catalog/vm"}],
        }
        payload = _profile_payload()
        payload["domains"][0]["evidence_refs"] = ["catalog_vm"]
        raw = run_profile_draft(
            pack,
            command=["true"],
            cwd=Path("/tmp"),
            runner=_draft_runner(payload),
        )
        declared = {source["id"]: source for source in raw["sources"]}
        self.assertIn("catalog_vm", declared)
        self.assertEqual(declared["catalog_vm"]["url"], "gapctl://catalog/vm")

    def test_partial_domain_mapping_rules_are_shared_with_the_generator(self) -> None:
        pack = {"project": {"declared_domains": ["vm"]}}
        captured: dict = {}

        def runner(command, cwd, request, env, timeout):
            del cwd, env, timeout
            captured.update(json.loads(request))
            return subprocess.CompletedProcess(command, 0, json.dumps(_profile_payload()), "")

        run_profile_draft(pack, command=["true"], cwd=Path("/tmp"), runner=runner)

        for rule in QUALIFICATION_MAPPING_RULES:
            self.assertIn(rule, captured["rules"])
        self.assertTrue(any("every check" in rule for rule in captured["rules"]))
        self.assertTrue(any("partially supported domain" in rule for rule in captured["rules"]))
        self.assertTrue(any("required behavior" in rule for rule in captured["rules"]))
        self.assertTrue(any("step and validation-class pair" in rule for rule in captured["rules"]))
        self.assertTrue(any("target version" in rule for rule in captured["rules"]))
        self.assertTrue(any("numeric nsrg_layers" in rule for rule in captured["rules"]))

    def test_draft_rejects_invented_or_missing_scope(self) -> None:
        pack = {"project": {"declared_domains": ["vm"]}}
        with self.assertRaises(QualifyError) as caught:
            run_profile_draft(
                pack,
                command=["true"],
                cwd=Path("/tmp"),
                runner=_draft_runner(_profile_payload(domains=("vm", "network"))),
            )
        self.assertIn("extra: network", str(caught.exception))
        with self.assertRaises(QualifyError):
            run_profile_draft(
                pack,
                command=["true"],
                cwd=Path("/tmp"),
                runner=_draft_runner(_profile_payload(domains=("network",))),
            )


class RatificationAidTests(unittest.TestCase):
    def test_empirical_conflicts_flag_covered_domains_with_failing_latest_run(self) -> None:
        profile = parse_solution_profile(_profile_payload())
        with tempfile.TemporaryDirectory() as tempdir:
            runs_root = Path(tempdir) / "runs"
            run_id, run_dir = new_run_dir(runs_root, "vm", created_at="20260101T000000Z")
            write_run_record(run_dir, run_id=run_id, domain="vm", config=None, exit_code=1)
            conflicts = empirical_conflicts(profile, runs_root)
            self.assertEqual(len(conflicts), 1)
            self.assertIn("vm", conflicts[0])

            run_id, run_dir = new_run_dir(runs_root, "vm", created_at="20260102T000000Z")
            write_run_record(run_dir, run_id=run_id, domain="vm", config=None, exit_code=0)
            self.assertEqual(empirical_conflicts(profile, runs_root), [])

    def test_profile_draft_diff_reports_per_domain_changes(self) -> None:
        current = parse_solution_profile(_profile_payload())
        draft = parse_solution_profile(_profile_payload(coverage="gap"))
        self.assertEqual(profile_draft_diff(current, draft), ["vm: coverage covered->gap"])
        self.assertEqual(profile_draft_diff(None, draft), ["vm: added (coverage=gap, owned=True)"])

    def test_effective_check_summary_counts_capabilities_over_domain_defaults(self) -> None:
        mapped_counts = {"vm": 27, "network": 27, "security": 28}
        raw = _profile_payload(tuple(mapped_counts))
        catalog_domains = {}
        for domain in raw["domains"]:
            name = domain["domain"]
            domain["coverage"] = "out_of_scope"
            domain["validation_mode"] = "skip"
            domain["capabilities"] = [
                {
                    "id": f"{name}-mapped",
                    "name": f"{name} mapped checks",
                    "selectors": {"validation_classes": ["MappedCheck*"]},
                    "coverage": "covered",
                    "validation_mode": "test",
                }
            ]
            checks = [
                {"name": f"MappedCheck{index}", "check": f"MappedCheck{index}"}
                for index in range(mapped_counts[name])
            ]
            checks.append({"name": "UnmatchedCheck", "check": "UnmatchedCheck"})
            catalog_domains[name] = {"checks": checks}

        summary = effective_check_summary(
            parse_solution_profile(raw),
            {"domains": catalog_domains},
        )

        self.assertEqual(summary[-1], "total: 82 covered/test, 3 out_of_scope/skip, 0 other (85 total)")


def _offline_fetcher(url: str, headers: Mapping[str, str]) -> bytes:
    del headers
    if url == DEFAULT_NSRG_URL:
        return b"- [Introduction](https://docs.nvidia.com/dsx/ncp/software-reference-guide/introduction.md):"
    if url.endswith("introduction.md"):
        return b"VM lifecycle reference guidance"
    if url == DEFAULT_INFERENCE_RA_URL:
        return b"# Complete Inference Reference Architecture\n\nInference validation guidance."
    raise OSError(f"network unreachable: {url}")


def _project(root: Path):
    workspace = root / "workspace"
    checkout = workspace / "ai-cloud-validation"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "isvctl" / "configs" / "providers" / "my-isv").mkdir(parents=True)
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


if __name__ == "__main__":
    unittest.main()
