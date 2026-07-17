from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

from isv_readiness.schema import load_schema

PROFILE_SCHEMA_VERSION = "0.1.0"

SUPPORTED_DOMAINS = frozenset(
    {
        "bare_metal",
        "control_plane",
        "iam",
        "image_registry",
        "kubernetes",
        "network",
        "observability",
        "security",
        "slurm",
        "vm",
    }
)

DOMAIN_ALIASES = {
    "bare-metal": "bare_metal",
    "control-plane": "control_plane",
    "image-registry": "image_registry",
    "k8s": "kubernetes",
}

Coverage = Literal["covered", "gap", "out_of_scope", "unknown"]
ValidationMode = Literal["test", "evidence", "skip", "deferred"]
AgentAction = Literal[
    "implement_or_fix_adapter",
    "request_external_adapter",
    "collect_evidence",
    "record_product_gap",
    "skip_with_rationale",
    "request_scope_decision",
]


class SolutionProfileError(ValueError):
    """Raised when a solution profile is invalid or resolves ambiguously."""


@dataclass(frozen=True)
class SourceReference:
    id: str
    title: str
    url: str
    kind: str


@dataclass(frozen=True)
class Actor:
    id: str
    name: str
    kind: str


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    version: str
    kind: str
    supplier_actor_id: str
    nsrg_layers: tuple[int, ...]
    depends_on: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ValidationSelector:
    steps: tuple[str, ...] = ()
    validation_categories: tuple[str, ...] = ()
    validation_classes: tuple[str, ...] = ()

    def matches(
        self,
        *,
        step_name: str | None,
        validation_category: str | None,
        validation_class: str | None,
    ) -> bool:
        return (
            _matches_dimension(self.steps, step_name)
            and _matches_dimension(self.validation_categories, validation_category)
            and _matches_dimension(self.validation_classes, validation_class)
        )

    @property
    def specificity(self) -> tuple[int, int]:
        dimensions = (self.steps, self.validation_categories, self.validation_classes)
        constrained_dimensions = sum(bool(values) for values in dimensions)
        exact_dimensions = sum(
            bool(values) and any("*" not in pattern and "?" not in pattern for pattern in values)
            for values in dimensions
        )
        return constrained_dimensions, exact_dimensions


@dataclass(frozen=True)
class CapabilityResponsibility:
    id: str
    name: str
    selector: ValidationSelector
    coverage: Coverage | None
    validation_mode: ValidationMode | None
    capability_owner_actor_id: str | None
    provider_adapter_owner_actor_id: str | None
    component_ids: tuple[str, ...]
    rationale: str
    required_inputs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class DomainResponsibility:
    domain: str
    name: str
    owned: bool
    coverage: Coverage
    validation_mode: ValidationMode
    capability_owner_actor_id: str
    provider_adapter_owner_actor_id: str
    component_ids: tuple[str, ...]
    provider_configs: tuple[str, ...]
    rationale: str
    required_inputs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    capabilities: tuple[CapabilityResponsibility, ...]


@dataclass(frozen=True)
class JourneyState:
    stage: str
    status: str


@dataclass(frozen=True)
class SolutionIdentity:
    id: str
    name: str
    vendor: str
    version: str
    profile_status: str
    target_environment: str


@dataclass(frozen=True)
class ResolvedResponsibility:
    domain: str
    capability_id: str
    coverage: Coverage
    validation_mode: ValidationMode
    capability_owner_actor_id: str
    provider_adapter_owner_actor_id: str
    component_ids: tuple[str, ...]
    rationale: str
    required_inputs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    action: AgentAction


@dataclass(frozen=True)
class SolutionProfile:
    schema_version: str
    solution: SolutionIdentity
    journey: JourneyState
    actors: tuple[Actor, ...]
    components: tuple[Component, ...]
    domains: tuple[DomainResponsibility, ...]
    sources: tuple[SourceReference, ...]
    assumptions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def resolve(
        self,
        domain: str,
        *,
        step_name: str | None = None,
        validation_category: str | None = None,
        validation_class: str | None = None,
    ) -> ResolvedResponsibility | None:
        canonical_domain = canonicalize_domain(domain)
        domain_scope = next((item for item in self.domains if item.domain == canonical_domain), None)
        if domain_scope is None:
            return None

        matches = [
            capability
            for capability in domain_scope.capabilities
            if capability.selector.matches(
                step_name=step_name,
                validation_category=validation_category,
                validation_class=validation_class,
            )
        ]
        capability: CapabilityResponsibility | None = None
        if matches:
            best_specificity = max(item.selector.specificity for item in matches)
            best_matches = [item for item in matches if item.selector.specificity == best_specificity]
            if len(best_matches) > 1:
                ids = ", ".join(sorted(item.id for item in best_matches))
                raise SolutionProfileError(
                    f"Ambiguous responsibility for domain '{canonical_domain}': {ids}"
                )
            capability = best_matches[0]

        coverage = capability.coverage if capability and capability.coverage else domain_scope.coverage
        validation_mode = (
            capability.validation_mode
            if capability and capability.validation_mode
            else domain_scope.validation_mode
        )
        capability_owner = (
            capability.capability_owner_actor_id
            if capability and capability.capability_owner_actor_id
            else domain_scope.capability_owner_actor_id
        )
        adapter_owner = (
            capability.provider_adapter_owner_actor_id
            if capability and capability.provider_adapter_owner_actor_id
            else domain_scope.provider_adapter_owner_actor_id
        )
        component_ids = (
            capability.component_ids if capability and capability.component_ids else domain_scope.component_ids
        )
        rationale = capability.rationale if capability and capability.rationale else domain_scope.rationale
        required_inputs = (
            capability.required_inputs
            if capability and capability.required_inputs
            else domain_scope.required_inputs
        )
        evidence_refs = (
            capability.evidence_refs
            if capability and capability.evidence_refs
            else domain_scope.evidence_refs
        )
        actor_by_id = {actor.id: actor for actor in self.actors}
        action = _resolve_action(coverage, validation_mode, actor_by_id[adapter_owner])
        return ResolvedResponsibility(
            domain=canonical_domain,
            capability_id=capability.id if capability else f"{canonical_domain}.default",
            coverage=coverage,
            validation_mode=validation_mode,
            capability_owner_actor_id=capability_owner,
            provider_adapter_owner_actor_id=adapter_owner,
            component_ids=component_ids,
            rationale=rationale,
            required_inputs=required_inputs,
            evidence_refs=evidence_refs,
            action=action,
        )

    def scope_summary(self) -> dict[str, Any]:
        """Readiness of the ISV-owned scope to enter/complete the validate phase.

        Only domains the ISV owns are assessed; external-dependency domains owned
        by other actors are reported but never block the ISV's own validation.
        """
        owned_domains = [domain for domain in self.domains if domain.owned]
        coverage_counts = {
            coverage: sum(domain.coverage == coverage for domain in owned_domains)
            for coverage in ("covered", "gap", "out_of_scope", "unknown")
        }
        blocking_domains = sorted(
            domain.domain
            for domain in owned_domains
            if blocks_readiness(domain.coverage, domain.validation_mode)
        )
        blocking_capabilities = sorted(
            capability.id
            for domain in owned_domains
            for capability in domain.capabilities
            if blocks_readiness(
                capability.coverage or domain.coverage,
                capability.validation_mode or domain.validation_mode,
            )
        )
        return {
            "solution_id": self.solution.id,
            "journey_stage": self.journey.stage,
            "journey_status": self.journey.status,
            "owned_domains": sorted(domain.domain for domain in owned_domains),
            "coverage": coverage_counts,
            "blocking_domains": blocking_domains,
            "blocking_capabilities": blocking_capabilities,
            "validation_ready": bool(owned_domains) and not blocking_domains and not blocking_capabilities,
        }


def blocks_readiness(coverage: str, validation_mode: str) -> bool:
    """A row blocks owned-scope readiness unless it is proven or deliberately excluded.

    ``covered``+``test`` is the proven path; ``out_of_scope``+``skip`` is a
    signed scope decision, not a deficiency, so it must not block forever.
    Everything else (gaps, unknowns, half-pairings) still blocks.
    """
    if coverage == "covered" and validation_mode == "test":
        return False
    if coverage == "out_of_scope" and validation_mode == "skip":
        return False
    return True


def profile_is_ratified(profile: SolutionProfile) -> bool:
    """Whether a human-reviewed profile has entered the validate phase."""

    return (
        profile.solution.profile_status in {"reviewed", "confirmed"}
        and profile.journey.stage == "validate"
    )


def load_solution_profile(path: Path, *, schema_path: Path | None = None) -> SolutionProfile:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, Mapping):
        raise SolutionProfileError(f"Solution profile must contain a mapping: {path}")
    return parse_solution_profile(payload, schema_path=schema_path)


def parse_solution_profile(
    payload: Mapping[str, Any], *, schema_path: Path | None = None
) -> SolutionProfile:
    try:
        schema = (
            yaml.safe_load(schema_path.read_text(encoding="utf-8"))
            if schema_path is not None
            else load_schema("solution-profile.schema.json")
        )
        jsonschema.Draft202012Validator(schema).validate(dict(payload))
    except (OSError, yaml.YAMLError, jsonschema.ValidationError) as exc:
        raise SolutionProfileError(f"Invalid solution profile: {exc}") from exc

    solution_payload = payload["solution"]
    journey_payload = payload["journey"]
    actors = tuple(
        Actor(id=item["id"], name=item["name"], kind=item["kind"])
        for item in payload["actors"]
    )
    components = tuple(_parse_component(item) for item in payload["components"])
    domains = tuple(_parse_domain(item) for item in payload["domains"])
    sources = tuple(
        SourceReference(
            id=item["id"],
            title=item["title"],
            url=item["url"],
            kind=item["kind"],
        )
        for item in payload.get("sources", [])
    )
    profile = SolutionProfile(
        schema_version=payload["schema_version"],
        solution=SolutionIdentity(
            id=solution_payload["id"],
            name=solution_payload["name"],
            vendor=solution_payload["vendor"],
            version=solution_payload["version"],
            profile_status=solution_payload["profile_status"],
            target_environment=solution_payload["target_environment"],
        ),
        journey=JourneyState(stage=journey_payload["stage"], status=journey_payload["status"]),
        actors=actors,
        components=components,
        domains=domains,
        sources=sources,
        assumptions=tuple(payload.get("assumptions", [])),
    )
    _validate_references(profile)
    return profile


def canonicalize_domain(domain: str) -> str:
    normalized = domain.strip().lower().replace(" ", "_")
    return DOMAIN_ALIASES.get(normalized, normalized)


def _parse_component(item: Mapping[str, Any]) -> Component:
    return Component(
        id=item["id"],
        name=item["name"],
        version=item["version"],
        kind=item["kind"],
        supplier_actor_id=item["supplier_actor_id"],
        nsrg_layers=tuple(item.get("nsrg_layers", ())),
        depends_on=tuple(item.get("depends_on", [])),
        source_refs=tuple(item.get("source_refs", [])),
    )


def _parse_domain(item: Mapping[str, Any]) -> DomainResponsibility:
    return DomainResponsibility(
        domain=canonicalize_domain(item["domain"]),
        name=item["name"],
        owned=bool(item.get("owned", True)),
        coverage=item["coverage"],
        validation_mode=item["validation_mode"],
        capability_owner_actor_id=item["capability_owner_actor_id"],
        provider_adapter_owner_actor_id=item["provider_adapter_owner_actor_id"],
        component_ids=tuple(item.get("component_ids", [])),
        provider_configs=tuple(item.get("provider_configs", [])),
        rationale=item.get("rationale", ""),
        required_inputs=tuple(item.get("required_inputs", [])),
        evidence_refs=tuple(item.get("evidence_refs", [])),
        capabilities=tuple(_parse_capability(capability) for capability in item.get("capabilities", [])),
    )


def _parse_capability(item: Mapping[str, Any]) -> CapabilityResponsibility:
    selector = item["selectors"]
    return CapabilityResponsibility(
        id=item["id"],
        name=item["name"],
        selector=ValidationSelector(
            steps=tuple(selector.get("steps", [])),
            validation_categories=tuple(selector.get("validation_categories", [])),
            validation_classes=tuple(selector.get("validation_classes", [])),
        ),
        coverage=item.get("coverage"),
        validation_mode=item.get("validation_mode"),
        capability_owner_actor_id=item.get("capability_owner_actor_id"),
        provider_adapter_owner_actor_id=item.get("provider_adapter_owner_actor_id"),
        component_ids=tuple(item.get("component_ids", [])),
        rationale=item.get("rationale", ""),
        required_inputs=tuple(item.get("required_inputs", [])),
        evidence_refs=tuple(item.get("evidence_refs", [])),
    )


def _validate_references(profile: SolutionProfile) -> None:
    actor_ids = _unique_ids(profile.actors, "actor")
    component_ids = _unique_ids(profile.components, "component")
    source_ids = _unique_ids(profile.sources, "source")

    domain_names = [domain.domain for domain in profile.domains]
    if len(domain_names) != len(set(domain_names)):
        raise SolutionProfileError("Solution profile contains duplicate domains")
    unsupported = sorted(set(domain_names).difference(SUPPORTED_DOMAINS))
    if unsupported:
        raise SolutionProfileError(f"Unsupported validation domains: {', '.join(unsupported)}")

    for component in profile.components:
        _require_reference(component.supplier_actor_id, actor_ids, f"component '{component.id}' actor")
        for dependency in component.depends_on:
            _require_reference(dependency, component_ids, f"component '{component.id}' dependency")
        for source_ref in component.source_refs:
            _require_reference(source_ref, source_ids, f"component '{component.id}' source")
    _validate_component_graph(profile.components)

    capability_ids: set[str] = set()
    for domain in profile.domains:
        _require_reference(
            domain.capability_owner_actor_id,
            actor_ids,
            f"domain '{domain.domain}' capability owner",
        )
        _require_reference(
            domain.provider_adapter_owner_actor_id,
            actor_ids,
            f"domain '{domain.domain}' adapter owner",
        )
        _validate_scope_references(
            domain.domain,
            domain.component_ids,
            domain.evidence_refs,
            component_ids,
            source_ids,
        )
        _require_rationale(domain.domain, domain.coverage, domain.validation_mode, domain.rationale)
        for capability in domain.capabilities:
            if capability.id in capability_ids:
                raise SolutionProfileError(f"Duplicate capability id: {capability.id}")
            capability_ids.add(capability.id)
            if capability.capability_owner_actor_id:
                _require_reference(
                    capability.capability_owner_actor_id,
                    actor_ids,
                    f"capability '{capability.id}' capability owner",
                )
            if capability.provider_adapter_owner_actor_id:
                _require_reference(
                    capability.provider_adapter_owner_actor_id,
                    actor_ids,
                    f"capability '{capability.id}' adapter owner",
                )
            _validate_scope_references(
                capability.id,
                capability.component_ids,
                capability.evidence_refs,
                component_ids,
                source_ids,
            )
            effective_coverage = capability.coverage or domain.coverage
            effective_mode = capability.validation_mode or domain.validation_mode
            effective_rationale = capability.rationale or domain.rationale
            _require_rationale(capability.id, effective_coverage, effective_mode, effective_rationale)


def _validate_scope_references(
    scope_id: str,
    scoped_components: tuple[str, ...],
    scoped_sources: tuple[str, ...],
    component_ids: set[str],
    source_ids: set[str],
) -> None:
    for component_id in scoped_components:
        _require_reference(component_id, component_ids, f"scope '{scope_id}' component")
    for source_id in scoped_sources:
        _require_reference(source_id, source_ids, f"scope '{scope_id}' evidence")


def _validate_component_graph(components: tuple[Component, ...]) -> None:
    dependencies = {component.id: component.depends_on for component in components}
    visited: set[str] = set()
    active: list[str] = []

    def visit(component_id: str) -> None:
        if component_id in visited:
            return
        if component_id in active:
            cycle_start = active.index(component_id)
            cycle = " -> ".join([*active[cycle_start:], component_id])
            raise SolutionProfileError(f"Component dependency cycle: {cycle}")
        active.append(component_id)
        for dependency in dependencies[component_id]:
            visit(dependency)
        active.pop()
        visited.add(component_id)

    for component_id in dependencies:
        visit(component_id)


def _unique_ids(items: tuple[Any, ...], label: str) -> set[str]:
    ids = [item.id for item in items]
    if len(ids) != len(set(ids)):
        raise SolutionProfileError(f"Solution profile contains duplicate {label} ids")
    return set(ids)


def _require_reference(value: str, choices: set[str], label: str) -> None:
    if value not in choices:
        raise SolutionProfileError(f"Unknown {label} reference: {value}")


def _require_rationale(
    scope_id: str,
    coverage: Coverage,
    validation_mode: ValidationMode,
    rationale: str,
) -> None:
    if (coverage != "covered" or validation_mode != "test") and not rationale.strip():
        raise SolutionProfileError(
            f"Scope '{scope_id}' requires a rationale for coverage '{coverage}' "
            f"and validation mode '{validation_mode}'"
        )


def _matches_dimension(patterns: tuple[str, ...], value: str | None) -> bool:
    if not patterns:
        return True
    return value is not None and any(fnmatchcase(value, pattern) for pattern in patterns)


def _resolve_action(
    coverage: Coverage,
    validation_mode: ValidationMode,
    adapter_owner: Actor,
) -> AgentAction:
    if coverage == "gap":
        return "record_product_gap"
    if coverage == "out_of_scope" or validation_mode == "skip":
        return "skip_with_rationale"
    if coverage == "unknown" or validation_mode == "deferred":
        return "request_scope_decision"
    if validation_mode == "evidence":
        return "collect_evidence"
    if adapter_owner.kind == "isv":
        return "implement_or_fix_adapter"
    return "request_external_adapter"
