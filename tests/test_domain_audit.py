from __future__ import annotations

import unittest

from isv_readiness.domain_audit import (
    ApprovedCapability,
    DomainAuditError,
    _parse_domain_audit,
)


class DomainAuditContractTests(unittest.TestCase):
    def test_rejects_an_audit_that_silently_omits_an_approved_capability(self) -> None:
        capabilities = (
            _capability("managed-slurm"),
            _capability("cluster-access"),
        )
        raw = _raw_audit("managed-slurm")

        with self.assertRaisesRegex(DomainAuditError, "every approved capability"):
            _parse_domain_audit(
                raw,
                domain="slurm",
                audit_context_sha256="a" * 64,
                capabilities=capabilities,
                source_paths={"scripts/slurm/setup.sh"},
                editable_targets={"scripts/slurm/setup.sh"},
            )

    def test_rejects_file_presence_as_an_implemented_claim_without_code_evidence(self) -> None:
        raw = _raw_audit("managed-slurm")
        raw["capabilities"][0]["status"] = "implemented"
        raw["capabilities"][0]["target"] = None

        with self.assertRaisesRegex(DomainAuditError, "without code evidence"):
            _parse_domain_audit(
                raw,
                domain="slurm",
                audit_context_sha256="a" * 64,
                capabilities=(_capability("managed-slurm"),),
                source_paths={"scripts/slurm/setup.sh"},
                editable_targets={"scripts/slurm/setup.sh"},
            )


def _capability(capability_id: str) -> ApprovedCapability:
    return ApprovedCapability(
        capability_id=capability_id,
        name=capability_id,
        selectors={},
        rationale="Approved managed lifecycle",
        required_inputs=(),
        evidence_refs=(),
        component_ids=(),
        capability_owner_actor_id="isv",
        provider_adapter_owner_actor_id="isv",
        action="implement_or_fix_adapter",
    )


def _raw_audit(capability_id: str) -> dict:
    return {
        "schema_version": "0.1.0",
        "domain": "slurm",
        "audit_context_sha256": "a" * 64,
        "auditor": {"adapter": "fixture", "model": "fixture"},
        "summary": "Audit Slurm",
        "capabilities": [
            {
                "capability_id": capability_id,
                "status": "gap",
                "step_name": "setup",
                "target": "scripts/slurm/setup.sh",
                "expected_effects": {
                    "setup": ["create cluster", "obtain access"],
                    "test": ["exercise workload"],
                    "teardown": ["delete owned cluster"],
                },
                "implementation_evidence": [],
                "reason": "The script only inventories a preexisting cluster.",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
