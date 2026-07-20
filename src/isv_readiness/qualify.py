"""Qualify-phase drafting: suite catalog, profile draft, and SME ratification aids.

The mapping target (which checks and test ids each NSRG domain demands) is
distilled from the pinned ai-cloud-validation checkout through the existing
isvctl plan contract — never fetched at ISV runtime. The drafting agent
proposes a solution profile from that catalog plus packed evidence; the SME
ratifies by editing the draft and flipping ``profile_status`` off ``draft``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from isv_readiness.changes import canonical_sha256
from isv_readiness.context import QUALIFICATION_MAPPING_RULES
from isv_readiness.generation import DEFAULT_GENERATOR_TIMEOUT_SECONDS, GeneratorRunner, dispatch_generator
from isv_readiness.runs import latest_run
from isv_readiness.schema import load_schema
from isv_readiness.solution_profile import (
    SolutionProfile,
    canonicalize_domain,
    parse_solution_profile,
)
from isv_readiness.validation_adapter import IsvctlAdapter

QUALIFY_CATALOG_SCHEMA_VERSION = "0.1.0"

# Suite filenames are not uniformly derivable from canonical domain names
# (bare_metal.yaml keeps its underscore, control-plane.yaml does not), so the
# mapping is explicit.
SUITE_FILENAMES = {
    "bare_metal": "bare_metal.yaml",
    "control_plane": "control-plane.yaml",
    "iam": "iam.yaml",
    "image_registry": "image-registry.yaml",
    "kubernetes": "k8s.yaml",
    "network": "network.yaml",
    "observability": "observability.yaml",
    "security": "security.yaml",
    "slurm": "slurm.yaml",
    "vm": "vm.yaml",
}


class QualifyError(ValueError):
    """Raised when the qualify catalog or profile draft cannot be produced safely."""


def build_qualify_catalog(adapter: IsvctlAdapter, domains: Sequence[str]) -> dict[str, Any]:
    """Distill the pinned suites into the mapping target for declared domains."""
    if not domains:
        raise QualifyError("At least one declared domain is required.")
    resolved: dict[str, Any] = {}
    isvctl_version: str | None = None
    for domain in domains:
        canonical = canonicalize_domain(domain)
        suite = SUITE_FILENAMES.get(canonical)
        if suite is None:
            raise QualifyError(f"No validation suite is known for domain '{domain}'.")
        if canonical in resolved:
            continue
        suite_path = f"isvctl/configs/suites/{suite}"
        plan = adapter.plan([suite_path])
        isvctl_version = isvctl_version or plan.isvctl_version
        checks = [
            {
                "name": validation.name,
                "check": validation.base_name,
                "test_id": _test_id(validation.params),
                "step": validation.step,
                "phase": validation.phase,
                "labels": list(validation.labels),
                "description": validation.description,
            }
            for validation in plan.validations
            if validation.valid
        ]
        if not checks:
            raise QualifyError(f"Suite '{suite_path}' yielded no valid checks for domain '{canonical}'.")
        resolved[canonical] = {
            "suite": suite_path,
            "checks": checks,
            "steps": sorted({validation.step for validation in plan.validations if validation.step}),
        }
    return {
        "schema_version": QUALIFY_CATALOG_SCHEMA_VERSION,
        "isvctl_version": isvctl_version,
        "domains": resolved,
    }


def run_profile_draft(
    pack: dict[str, Any],
    *,
    command: Sequence[str],
    cwd: Path,
    pass_env: Sequence[str] = (),
    timeout_seconds: int = DEFAULT_GENERATOR_TIMEOUT_SECONDS,
    runner: GeneratorRunner | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Draft a solution profile from the qualify pack via a generator adapter.

    The returned document is deterministically hardened: it is always a
    ``draft`` in the ``qualify`` stage regardless of what the model claims,
    and its domain set must exactly match the declared scope.
    """
    request = {
        "schema_version": "0.1.0",
        "task": (
            "Draft a qualify-phase solution profile mapping the provider's evidenced "
            "capabilities onto the declared validation domains."
        ),
        "context_pack_sha256": canonical_sha256(pack),
        "rules": [
            "Return one JSON object and no Markdown or commentary.",
            "Draft every declared domain and no others; never invent scope.",
            *QUALIFICATION_MAPPING_RULES,
            "Use unknown or deferred when the capability mapping is unclear, and use gap only when the supplied ISV evidence explicitly shows a required capability is absent.",
            "State the supporting evidence for each domain in its 'rationale'.",
            "Every id used in 'evidence_refs' must also be declared in the profile's 'sources' list.",
            "Model context_pack.project.provider as an actor of kind 'isv' and default both capability_owner_actor_id and provider_adapter_owner_actor_id to it for owned domains; add other actors only when the evidence names them.",
            "Ownership fields are suggestions for SME review, not decisions.",
            "Copy environment facts (versions, addresses, CIDRs) verbatim from the evidence; do not infer them.",
            "Do not include credential values anywhere.",
        ],
        "output_schema": load_schema("solution-profile.schema.json"),
        "context_pack": pack,
    }
    raw = dispatch_generator(
        request,
        command=command,
        cwd=cwd,
        pass_env=pass_env,
        timeout_seconds=timeout_seconds,
        runner=runner,
        environment=environment,
    )
    if isinstance(raw.get("solution"), dict):
        raw["solution"]["profile_status"] = "draft"
    raw["journey"] = {"stage": "qualify", "status": "in_progress"}
    _declare_cited_pack_items(raw, pack)
    profile = parse_solution_profile(raw)

    declared = {canonicalize_domain(domain) for domain in pack["project"]["declared_domains"]}
    drafted = {domain.domain for domain in profile.domains}
    if drafted != declared:
        extra = ", ".join(sorted(drafted - declared)) or "none"
        missing = ", ".join(sorted(declared - drafted)) or "none"
        raise QualifyError(f"Draft domains must exactly match the declared scope (extra: {extra}; missing: {missing}).")
    return raw


def _declare_cited_pack_items(raw: dict[str, Any], pack: Mapping[str, Any]) -> None:
    """Declare pack items the draft cites as evidence in its sources list.

    Citing the packed catalog/spec/run excerpts is exactly the evidence
    discipline the rules demand; the profile's referential-integrity check
    should not fail a draft for doing it.
    """
    items = {
        item["source_id"]: item
        for item in pack.get("items", ())
        if isinstance(item, Mapping) and isinstance(item.get("source_id"), str)
    }
    declared = {source.get("id") for source in raw.get("sources") or () if isinstance(source, Mapping)}
    cited: set[str] = set()
    for domain in raw.get("domains") or ():
        if not isinstance(domain, Mapping):
            continue
        cited.update(ref for ref in domain.get("evidence_refs") or () if isinstance(ref, str))
        for capability in domain.get("capabilities") or ():
            if isinstance(capability, Mapping):
                cited.update(ref for ref in capability.get("evidence_refs") or () if isinstance(ref, str))
    for ref in sorted(cited - declared):
        if ref in items:
            raw.setdefault("sources", []).append(
                {
                    "id": ref,
                    "title": f"qualify evidence pack item {ref}",
                    "url": str(items[ref].get("origin") or f"gapctl://pack/{ref}"),
                    "kind": "other",
                }
            )


def empirical_conflicts(profile: SolutionProfile, runs_root: Path) -> list[str]:
    """Domains claimed covered whose latest recorded run failed."""
    conflicts = []
    for domain in profile.domains:
        if not domain.owned or domain.coverage != "covered":
            continue
        run = latest_run(runs_root, domain.domain)
        if run is not None and isinstance(run.exit_code, int) and run.exit_code != 0:
            conflicts.append(
                f"{domain.domain}: claimed 'covered' but latest recorded run {run.run_id} exited {run.exit_code}"
            )
    return conflicts


def profile_draft_diff(current: SolutionProfile | None, draft: SolutionProfile) -> list[str]:
    """Per-domain differences between the current profile and a draft."""
    current_domains = {domain.domain: domain for domain in current.domains} if current else {}
    draft_domains = {domain.domain: domain for domain in draft.domains}
    lines = []
    for name in sorted(current_domains.keys() | draft_domains.keys()):
        before, after = current_domains.get(name), draft_domains.get(name)
        if before is None:
            lines.append(f"{name}: added (coverage={after.coverage}, owned={after.owned})")
            continue
        if after is None:
            lines.append(f"{name}: removed")
            continue
        changes = [
            f"{field} {getattr(before, field)}->{getattr(after, field)}"
            for field in ("coverage", "owned", "validation_mode")
            if getattr(before, field) != getattr(after, field)
        ]
        if changes:
            lines.append(f"{name}: " + ", ".join(changes))
    return lines


def _test_id(params: Any) -> str | None:
    if isinstance(params, Mapping) and isinstance(params.get("test_id"), str):
        return params["test_id"]
    return None
