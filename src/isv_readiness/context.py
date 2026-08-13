from __future__ import annotations

import ast
import builtins
import hashlib
import json
import math
import os
import re
import shlex
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import jsonschema
import yaml

from isv_readiness.decision import adapter_contract_unit, decide_gap
from isv_readiness.failure_feedback import redact_failure_text, redact_failure_value
from isv_readiness.project import (
    DEFAULT_INFERENCE_RA_URL,
    DEFAULT_NSRG_URL,
    MINIMAL_PROCESS_ENV,
    ContextSource,
    ReadinessProject,
    declared_provider_environment,
)
from isv_readiness.runs import latest_run
from isv_readiness.scan.models import Evidence, GapReport, GapRow, Remediation
from isv_readiness.scan.schema_registry import SchemaRegistry
from isv_readiness.schema import load_schema

CONTEXT_PACK_SCHEMA_VERSION = "0.1.0"
CONTEXT_CACHE_SCHEMA_VERSION = "0.3.0"
MAX_SOURCE_BYTES = 1_000_000
NCP_GUIDE_PAGE_PREFIXES = (
    "https://docs.nvidia.com/dsx/ncp/software-reference-guide/",
    "https://docs.nvidia.com/dsx/ncp/part-1-software-reference-guide/",
    "https://docs.nvidia.com/dsx/ncp/part-2-software-components/",
)
NCP_GUIDE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https://docs\.nvidia\.com/dsx/ncp/[^)]+\.md)\)")
MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
MERMAID_NODE_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9_]*)\s*\[(?:"([^"]*)"|([^\]]*))\]')
MERMAID_SUBGRAPH_RE = re.compile(r'^\s*subgraph\s+([A-Za-z][A-Za-z0-9_]*)\s*\[(?:"([^"]*)"|([^\]]*))\]')
LIFECYCLE_TIMEOUT_CONSTRAINT = "lifecycle_step_timeout_seconds"
TEXT_EXTENSIONS = {
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_FILENAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json"}

Fetcher = Callable[[str, Mapping[str, str]], bytes]
OUTCOME_CALLS = frozenset({"report_subtest", "set_failed", "set_passed"})
DYNAMIC_CALLS = frozenset({"__import__", "eval", "exec", "getattr", "globals", "locals"})
BUILTIN_CALLS = frozenset(dir(builtins))
MAX_DEPENDENCY_SOURCE_CHARS = 4_000


class ContextError(ValueError):
    """Raised when context cannot be collected or packed safely."""


@dataclass(frozen=True)
class ContextRecord:
    source_id: str
    kind: str
    trust: str
    origin: str
    status: str
    sha256: str | None
    content: Any
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextItem:
    source_id: str
    kind: str
    trust: str
    origin: str
    sha256: str
    content: str
    truncated: bool
    relevance: str


@dataclass(frozen=True)
class ContextPack:
    schema_version: str
    project: dict[str, Any]
    gap: dict[str, Any]
    credentials: dict[str, list[str]]
    constraints: tuple[str, ...]
    items: tuple[ContextItem, ...]
    budget: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def sync_context_sources(
    project: ReadinessProject,
    manifest_path: Path,
    cache_dir: Path,
    *,
    fetcher: Fetcher | None = None,
) -> tuple[ContextRecord, ...]:
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    records: list[ContextRecord] = []
    for source in project.context_sources:
        try:
            record = _sync_source(
                project,
                manifest_path,
                source,
                fetcher=fetcher or _fetch_url,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ContextError) as exc:
            record = ContextRecord(
                source_id=source.id,
                kind=source.kind,
                trust=source.trust,
                origin=source.location,
                status="error" if source.required else "missing",
                sha256=None,
                content=None,
                error=str(exc),
            )
        _write_record(cache_dir, record)
        records.append(record)
    declared_ids = {source.id for source in project.context_sources}
    for path in cache_dir.glob("*.json"):
        if path.name != "index.json" and path.stem not in declared_ids:
            path.unlink()
    index = {
        "schema_version": CONTEXT_CACHE_SCHEMA_VERSION,
        "source_definitions": {
            source.id: _jsonable(asdict(source)) for source in project.context_sources
        },
        "records": [record.to_dict() for record in records],
    }
    (cache_dir / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tuple(records)


def load_context_records(cache_dir: Path) -> tuple[ContextRecord, ...]:
    records: list[ContextRecord] = []
    if not cache_dir.exists():
        return ()
    for path in sorted(cache_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            records.append(ContextRecord(**raw))
        except (json.JSONDecodeError, KeyError, TypeError):
            # Stale or corrupt cache file; context_cache_is_current will detect the mismatch
            continue
    return tuple(records)


def context_cache_is_current(
    project: ReadinessProject,
    cache_dir: Path,
    *,
    manifest_path: Path | None = None,
) -> bool:
    """Return whether the cache still represents the declared context sources.

    A manifest path lets callers also compare local source content. Network
    sources remain pinned to their last successful sync until a normal refresh
    is otherwise required; checking freshness must not introduce hidden
    network access.
    """

    index_path = cache_dir / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if index.get("schema_version") != CONTEXT_CACHE_SCHEMA_VERSION:
        return False
    records = index.get("records")
    if not isinstance(records, list):
        return False
    expected_definitions = {
        source.id: _jsonable(asdict(source)) for source in project.context_sources
    }
    if index.get("source_definitions") != expected_definitions:
        return False
    cached_ids = {record.get("source_id") for record in records if isinstance(record, dict)}
    expected_ids = {source.id for source in project.context_sources}
    if cached_ids != expected_ids:
        return False

    indexed_records = {
        record["source_id"]: record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("source_id"), str)
    }
    for source_id in expected_ids:
        record_path = cache_dir / f"{source_id}.json"
        try:
            stored = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if stored != indexed_records.get(source_id):
            return False

    if manifest_path is None:
        return True

    for source in project.context_sources:
        is_url = source.location.startswith(("https://", "http://"))
        if source.kind == "web_url" or (source.kind == "api_spec" and is_url):
            continue
        try:
            current = _sync_source(
                project,
                manifest_path,
                source,
                fetcher=_unexpected_cache_check_fetch,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ContextError):
            return False
        cached = indexed_records[source.id]
        if current.sha256 != cached.get("sha256") or current.origin != cached.get("origin"):
            return False
    return True


def build_context_pack(
    project: ReadinessProject,
    manifest_path: Path,
    report: GapReport | dict[str, Any],
    *,
    gap_id: str,
    cache_dir: Path,
    environment: Mapping[str, str] | None = None,
    max_chars: int | None = None,
    feedback: Sequence[Mapping[str, Any] | str] = (),
    whole_domain: bool = False,
    provider_root_override: Path | None = None,
) -> ContextPack:
    if max_chars is not None and max_chars < 4_000:
        raise ContextError("Context budget must be at least 4000 characters.")
    rows = report.rows if isinstance(report, GapReport) else [_gap_from_dict(row) for row in report.get("rows", [])]
    gap = next((row for row in rows if row.id == gap_id), None)
    if gap is None:
        raise ContextError(f"Gap not found: {gap_id}")
    if gap.domain not in project.assessment.domains:
        raise ContextError(f"Gap domain '{gap.domain}' is outside the selected project scope.")

    env = environment if environment is not None else os.environ
    required_env = sorted(set(project.execution.credential_env))
    available_env = sorted(name for name in required_env if env.get(name))
    terms = _gap_terms(gap)
    candidates = _local_gap_items(
        project,
        manifest_path,
        gap,
        terms,
        whole_domain=whole_domain,
        provider_root_override=provider_root_override,
    )
    runtime_names = declared_provider_environment(project, gap.domain)
    candidates.append(
        _item(
            "provider_runtime_contract",
            "runtime_contract",
            "authoritative",
            "gapctl://project/runtime",
            json.dumps(
                {
                    "credential_env": list(project.execution.credential_env),
                    "pass_env": list(project.execution.pass_env),
                    "injected_interface_env": [
                        interface.base_url_env
                        for interface in project.interfaces
                        if interface.base_url
                        and interface.base_url_env
                        and gap.domain in interface.domains
                    ],
                    "minimal_process_env": list(MINIMAL_PROCESS_ENV),
                    "allowed_provider_env": list(runtime_names),
                    "available_env_names": [name for name in runtime_names if env.get(name)],
                },
                indent=2,
                sort_keys=True,
            ),
            "complete runtime input contract; names only, never values",
        )
    )
    selected_unit = adapter_contract_unit(gap.to_dict())
    related_rows = [
        row
        for row in rows
        if row.id != gap.id
        and row.domain == gap.domain
        and adapter_contract_unit(row.to_dict()) == selected_unit
        and decide_gap(row.to_dict()).edit_eligible
    ]
    related = [_related_gap_contract(row) for row in related_rows]
    if related:
        candidates.append(
            _item(
                "related_target_gaps",
                "validation_contract",
                "authoritative",
                "gapctl://selected-target/related-gaps",
                json.dumps(related, indent=2, sort_keys=True),
                "other edit-eligible unresolved validation rows sharing the selected adapter contract unit",
            )
        )
    domain_lifecycle = _domain_lifecycle_contract(rows, gap)
    if domain_lifecycle:
        candidates.append(
            _item(
                "domain_lifecycle_contract",
                "validation_contract",
                "authoritative",
                "gapctl://selected-domain/lifecycle",
                json.dumps(domain_lifecycle, indent=2, sort_keys=True),
                "complete ordered setup, test, and teardown map for the reviewed domain",
            )
        )
    contract_rows = (
        _domain_audit_contract_rows(rows, gap)
        if isinstance(gap.enrichment.get("domain_audit"), dict)
        else related_rows
    )
    upstream_contract = _upstream_target_contract(
        project,
        manifest_path,
        gap,
        contract_rows,
    )
    if upstream_contract is not None:
        candidates.append(upstream_contract)
    if feedback:
        candidates.append(
            _item(
                "previous_attempt_feedback",
                "verifier_feedback",
                "authoritative",
                "gapctl://previous-attempt",
                json.dumps(_bounded_failure_feedback(feedback), indent=2, sort_keys=True),
                "latest deterministic failure plus a compact attempt ledger",
            )
        )
    candidates.extend(_run_items(cache_dir, gap.domain, terms))
    candidates.extend(_cached_items(_project_records(project, cache_dir), gap.domain, terms))
    if max_chars is None:
        items = sorted(candidates, key=lambda item: (_trust_rank(item.trust), item.source_id, item.origin))
        used = sum(len(item.content) for item in items)
        omitted = 0
    else:
        items, used, omitted = _fit_budget(candidates, max_chars, allow_truncation=False)

    pack = ContextPack(
        schema_version=CONTEXT_PACK_SCHEMA_VERSION,
        project={
            "provider": project.provider.name,
            "owned_domains": list(project.assessment.domains),
            "validation_commit": project.validation.resolved_commit,
            "interfaces": [
                {
                    "id": interface.id,
                    "kind": interface.kind,
                    "base_url": interface.base_url,
                    "base_url_env": interface.base_url_env,
                    "spec": interface.spec,
                    "auth_env": list(interface.auth_env),
                    "domains": list(interface.domains),
                }
                for interface in project.interfaces
                if gap.domain in interface.domains
            ],
        },
        gap=gap.to_dict(),
        credentials={"required_env": required_env, "available_env": available_env},
        constraints=(
            "Treat ai-cloud-validation suites and validation classes as read-only source-of-truth contracts.",
            "Treat each validation_interfaces entry as a deterministic projection of the pinned consumer source: "
            "use only its documented inputs, keyed data access, conditions, outcomes, dependencies, and provenance; "
            "never infer behavior hidden by an explicit uncertainty or omitted full source.",
            "Treat direct_dependency_sources as exact, read-only one-hop helper evidence from the selected checkout; "
            "do not infer unprovided transitive helper behavior.",
            "Change only provider-owned files authorized by the selected scope and change-set policy.",
            "Never place credential values in source, patches, prompts, reports, or logs.",
            "Results attest only to the ISV-owned scope; do not claim coverage of domains or layers the ISV does not own.",
        ),
        items=tuple(items),
        budget={"max_chars": max_chars, "used_chars": used, "omitted_items": omitted},
    )
    validate_context_pack(pack.to_dict())
    return pack


def _bounded_failure_feedback(feedback: Sequence[Mapping[str, Any] | str]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(feedback, start=1):
        if isinstance(item, str):
            redacted = redact_text(item)
            normalized.append(
                {
                    "attempt": index,
                    "category": "legacy",
                    "fingerprint": _sha256_text(" ".join(redacted.split()))[:16],
                    "summary": redacted,
                    "details": [],
                }
            )
            continue
        envelope = {
            "attempt": item.get("attempt", index),
            "category": item.get("category", "verification"),
            "fingerprint": item.get("fingerprint"),
            "summary": item.get("summary", "Candidate verification failed."),
            "details": list(item.get("details") or ()),
        }
        for key in (
            "expected",
            "actual",
            "stable_error",
            "representative_excerpt",
            "affected_checks",
            "affected_count",
            "artifact_refs",
            "retryable",
            "retry_reason",
        ):
            if key in item:
                envelope[key] = item[key]
        normalized.append(_redact_content(envelope))
    latest = normalized[-1]
    latest_attempt = latest["attempt"]
    return {
        "latest": latest,
        "root_causes": [
            item for item in normalized
            if item["attempt"] == latest_attempt
        ],
        "ledger": [
            {
                "attempt": item["attempt"],
                "category": item["category"],
                "fingerprint": item["fingerprint"],
                "summary": item["summary"],
                "affected_checks": item.get("affected_checks", []),
                "affected_count": item.get("affected_count", 0),
                "retryable": item.get("retryable"),
            }
            for item in normalized
        ],
    }


def build_qualify_pack(
    project: ReadinessProject,
    catalog: Mapping[str, Any],
    *,
    cache_dir: Path,
) -> dict[str, Any]:
    """Build the profile-scoped evidence pack for qualify-phase drafting.

    Unlike the per-gap pack this covers every declared domain at once: the
    suite catalog (what NVIDIA demands), the cached API spec and guidance
    (what the ISV exposes), and the latest recorded run per domain.
    """
    domains = catalog.get("domains")
    if not isinstance(domains, Mapping) or not domains:
        raise ContextError("Qualify catalog contains no domains.")

    contents = {domain: json.dumps(entry, indent=2, sort_keys=True) for domain, entry in domains.items()}
    terms = _tokenize(" ".join(domains))
    for content in contents.values():
        terms |= _tokenize(content)
    candidates: list[ContextItem] = []
    for domain, content in sorted(contents.items()):
        candidates.append(
            _item(
                f"catalog_{domain}",
                "suite_catalog",
                "authoritative",
                f"gapctl://catalog/{domain}",
                content,
                f"validation suite contract for declared domain {domain}",
            )
        )
        candidates.extend(_run_items(cache_dir, domain, terms))
    candidates.extend(
        _cached_items(
            _qualify_records(project, cache_dir),
            "declared scope",
            terms,
            preserve_reference=True,
        )
    )
    items = sorted(candidates, key=lambda item: (_trust_rank(item.trust), item.source_id, item.origin))
    used = sum(len(item.content) for item in items)

    pack = {
        "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
        "purpose": "qualify_draft",
        "project": {
            "provider": project.provider.name,
            "declared_domains": sorted(domains),
            "validation_commit": project.validation.resolved_commit,
            "interfaces": [
                {
                    "id": interface.id,
                    "kind": interface.kind,
                    "base_url": interface.base_url,
                    "base_url_env": interface.base_url_env,
                    "spec": interface.spec,
                    "auth_env": list(interface.auth_env),
                    "domains": list(interface.domains),
                }
                for interface in project.interfaces
            ],
        },
        "constraints": [
            "Treat ai-cloud-validation suites and validation classes as read-only source-of-truth contracts.",
            "For each Inference Reference Architecture Mermaid visual, read the appended agent-readable description together with the preserved original diagram. Treat both as reference guidance, never as empirical or authoritative ISV evidence.",
        ],
        "items": [_jsonable(asdict(item)) for item in items],
        "budget": {"max_chars": None, "used_chars": used, "omitted_items": 0},
    }
    _validate_against_schema(pack, "qualify-pack.schema.json", "qualify pack")
    return pack


def validate_context_pack(raw: Any) -> None:
    _validate_against_schema(raw, "context-pack.schema.json", "context pack")


def provider_contract_constraints(context_pack: Mapping[str, Any]) -> dict[str, float]:
    """Normalize optional machine-readable limits from authoritative API specs."""

    values: list[float] = []
    for item in context_pack.get("items", []):
        if not isinstance(item, Mapping) or item.get("kind") != "api_spec" or item.get("trust") != "authoritative":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ContextError("Authoritative API specification is not valid YAML or JSON.") from exc
        if not isinstance(parsed, Mapping):
            continue
        runtime = parsed.get("runtime")
        timing = runtime.get("operation_timing") if isinstance(runtime, Mapping) else None
        value = timing.get(LIFECYCLE_TIMEOUT_CONSTRAINT) if isinstance(timing, Mapping) else None
        if value is None:
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value <= 0
            or value > 86_400
        ):
            raise ContextError(f"Authoritative {LIFECYCLE_TIMEOUT_CONSTRAINT} must be a number from 1 through 86400.")
        values.append(float(value))

    if not values:
        return {}
    if len(set(values)) != 1:
        raise ContextError("Authoritative API specifications declare conflicting lifecycle timeout thresholds.")
    return {LIFECYCLE_TIMEOUT_CONSTRAINT: values[0]}


def _validate_against_schema(raw: Any, schema_name: str, label: str) -> None:
    try:
        jsonschema.validate(raw, load_schema(schema_name))
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or label.replace(" ", "_")
        raise ContextError(f"Invalid {label} at {location}: {exc.message}") from exc


def _fit_budget(
    candidates: list[ContextItem],
    max_chars: int,
    *,
    allow_truncation: bool = True,
) -> tuple[list[ContextItem], int, int]:
    candidates = sorted(candidates, key=lambda item: (_trust_rank(item.trust), item.source_id, item.origin))
    items: list[ContextItem] = []
    used = 0
    omitted = 0
    for item in candidates:
        remaining = max_chars - used
        if remaining <= 0:
            if not allow_truncation:
                raise ContextError(
                    f"Context exceeds {max_chars} characters before source '{item.source_id}'; "
                    "refusing to omit evidence."
                )
            omitted += 1
            continue
        content = item.content
        truncated = item.truncated
        if len(content) > remaining:
            if not allow_truncation:
                raise ContextError(
                    f"Context exceeds {max_chars} characters at source '{item.source_id}'; "
                    "refusing to truncate evidence."
                )
            if remaining < 256:
                omitted += 1
                continue
            content = content[:remaining]
            truncated = True
        items.append(
            ContextItem(
                source_id=item.source_id,
                kind=item.kind,
                trust=item.trust,
                origin=item.origin,
                sha256=_sha256_text(content),
                content=content,
                truncated=truncated,
                relevance=item.relevance,
            )
        )
        used += len(content)
    return items, used, omitted


def _related_gap_contract(row: GapRow) -> dict[str, Any]:
    """Keep sibling requirements useful without repeating the full scan record."""
    return {
        "id": row.id,
        "step_name": row.step_name,
        "validation_class": row.validation_class,
        "requirement_id": row.requirement_id,
        "status": row.status,
        "detection": row.detection,
        "evidence": {
            "message": row.evidence.message,
            "validation_message": row.evidence.validation_message,
            "schema_errors": list(row.evidence.schema_errors),
            "missing_json_fields": list(row.evidence.missing_json_fields),
        },
        "labels": list(row.labels),
    }


def _domain_lifecycle_contract(rows: Sequence[GapRow], gap: GapRow) -> list[dict[str, Any]]:
    """Summarize every configured contract row in domain execution order."""

    steps: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.domain != gap.domain:
            continue
        phase = row.enrichment.get("validation_phase")
        profile = row.enrichment.get("solution_profile")
        profile = profile if isinstance(profile, dict) else {}
        entry = steps.setdefault(
            row.step_name,
            {
                "step_name": row.step_name,
                "phase": phase if isinstance(phase, str) else None,
                "selected": row.step_name == gap.step_name,
                "target": row.remediation.target,
                "statuses": [],
                "checks": [],
                "reviewed_capabilities": [],
            },
        )
        if entry["phase"] is None and isinstance(phase, str):
            entry["phase"] = phase
        entry["selected"] = bool(entry["selected"] or row.id == gap.id)
        if entry["target"] is None and row.remediation.target:
            entry["target"] = row.remediation.target
        if row.status not in entry["statuses"]:
            entry["statuses"].append(row.status)
        check = row.validation_class or row.requirement_id
        if check and check not in entry["checks"]:
            entry["checks"].append(check)
        capability = {
            key: profile.get(key)
            for key in (
                "capability_id",
                "coverage",
                "validation_mode",
                "action",
                "required_inputs",
            )
            if profile.get(key) is not None
        }
        if capability and capability not in entry["reviewed_capabilities"]:
            entry["reviewed_capabilities"].append(capability)
    return list(steps.values())


def _domain_audit_contract_rows(rows: Sequence[GapRow], gap: GapRow) -> list[GapRow]:
    """Attach real consumer rows for the capability behind a semantic audit gap."""

    audit = gap.enrichment.get("domain_audit")
    audit = audit if isinstance(audit, dict) else {}
    capability_id = str(audit.get("capability_id") or gap.requirement_id or "")
    selected: list[GapRow] = []
    for row in rows:
        if row.id == gap.id or row.domain != gap.domain:
            continue
        profile = row.enrichment.get("solution_profile")
        profile = profile if isinstance(profile, dict) else {}
        phase = row.enrichment.get("validation_phase")
        if profile.get("capability_id") == capability_id or phase in {"setup", "teardown"}:
            selected.append(row)
    return selected


def _upstream_target_contract(
    project: ReadinessProject,
    manifest_path: Path,
    gap: GapRow,
    related_rows: Sequence[GapRow],
) -> ContextItem | None:
    """Pack the exact suite entries and validation classes for one adapter contract unit."""

    validation_root = project.validation_root(manifest_path)
    if not validation_root.is_dir():
        return None
    rows = (gap, *related_rows)
    steps = {row.step_name for row in rows if row.step_name and not row.step_name.startswith("<")}
    classes = {
        row.validation_class
        for row in rows
        if row.validation_class
        and row.validation_class not in {"StepOutputSchema", "DomainLifecycleAudit"}
    }
    suite_name = "k8s.yaml" if gap.domain in {"k8s", "kubernetes"} else f"{gap.domain}.yaml"
    suite_path = validation_root / "isvctl" / "configs" / "suites" / suite_name
    suite_entries: dict[str, Any] = {}
    if suite_path.is_file():
        suite_raw = yaml.safe_load(_read_text(suite_path)) or {}
        validations = ((suite_raw.get("tests") or {}).get("validations") or {})
        if isinstance(validations, dict):
            suite_entries = _selected_suite_entries(validations, steps=steps, classes=classes)

    class_interfaces: dict[str, dict[str, Any]] = {}
    source_root = validation_root / "isvtest" / "src" / "isvtest" / "validations"
    if source_root.is_dir():
        for path in sorted(source_root.rglob("*.py")):
            text = _read_text(path)
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or node.name not in classes or node.name in class_interfaces:
                    continue
                source = ast.get_source_segment(text, node)
                if source:
                    class_interfaces[node.name] = _validation_class_interface(
                        node,
                        text,
                        path=str(path.relative_to(validation_root)),
                        source_sha256=_sha256_text(source),
                    )

    output_schemas: dict[str, Any] = {}
    registry = SchemaRegistry(validation_root)
    for step in sorted(steps):
        schema_name = registry.schema_for_step(step)
        schema = registry.schema(schema_name) if schema_name else None
        output_schemas[step] = {"name": schema_name, "schema": schema}

    dependency_sources = _direct_dependency_sources(validation_root, class_interfaces)

    payload = {
        "suite_path": str(suite_path.relative_to(validation_root)) if suite_path.is_file() else None,
        "suite_entries": suite_entries,
        "output_schemas": output_schemas,
        "validation_interface_projection": {
            "version": "python_ast_v1",
            "captures": [
                "class and method signatures",
                "docstrings and class attributes",
                "constant-string mapping lookups",
                "if, while, and assert conditions",
                "return expressions",
                "validation outcome calls",
                "direct call dependencies and caught exceptions",
                "one bounded source hop for uniquely resolved local helper calls",
                "dynamic-call uncertainty",
            ],
            "full_source_in_pack": False,
        },
        "validation_interfaces": class_interfaces,
        "direct_dependency_sources": dependency_sources,
        "missing_validation_classes": sorted(classes.difference(class_interfaces)),
    }
    if not suite_entries and not class_interfaces and not output_schemas:
        return None
    return _item(
        "upstream_target_contract",
        "validation_contract",
        "authoritative",
        "gapctl://upstream/selected-target",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "exact pinned suite entries and output schemas plus provenance-backed interfaces extracted from validation consumers",
    )


def _selected_suite_entries(
    validations: Mapping[str, Any],
    *,
    steps: set[str],
    classes: set[str],
) -> dict[str, Any]:
    """Preserve matching map- and list-shaped suite contracts exactly."""

    selected: dict[str, Any] = {}
    for name, value in validations.items():
        if (
            isinstance(value, dict)
            and value.get("step") in steps
            and isinstance(value.get("checks"), dict)
            and classes.intersection(value["checks"])
        ):
            selected[name] = value
            continue
        if not isinstance(value, list):
            continue
        matching_items: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            matching_checks = {
                class_name: parameters
                for class_name, parameters in item.items()
                if class_name in classes
                and isinstance(parameters, dict)
                and parameters.get("step") in steps
            }
            if matching_checks:
                matching_items.append(matching_checks)
        if matching_items:
            selected[name] = matching_items
    return selected


def _validation_class_interface(
    node: ast.ClassDef,
    text: str,
    *,
    path: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Project one validation class into deterministic, provenance-backed facts.

    Full consumer source remains authoritative at ``path`` and ``source_sha256``.
    The model receives only the interface facts needed to implement a provider
    adapter, which avoids treating unrelated consumer implementation details as
    provider requirements.
    """

    methods = [
        _method_interface(child, text)
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    lookups: list[dict[str, Any]] = []
    calls: dict[str, set[int]] = {}
    conditions: list[dict[str, Any]] = []
    returns: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            callee = _call_name(child.func)
            if _is_direct_dependency(callee):
                calls.setdefault(callee or "<dynamic>", set()).add(child.lineno)
            if callee and callee.rsplit(".", 1)[-1] in OUTCOME_CALLS:
                outcome: dict[str, Any] = {
                    "line": child.lineno,
                    "callee": callee,
                }
                if callee.endswith("report_subtest") and child.args:
                    outcome["subtest"] = _bounded_expression(text, child.args[0])
                outcomes.append(outcome)
            if not callee or callee.rsplit(".", 1)[-1] in DYNAMIC_CALLS:
                uncertainties.append(
                    {
                        "line": child.lineno,
                        "reason": "dynamic call cannot be resolved statically",
                        "expression": _source_expression(text, child.func),
                    }
                )
            if (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "get"
                and child.args
                and isinstance(child.args[0], ast.Constant)
                and isinstance(child.args[0].value, str)
            ):
                lookups.append(
                    {
                        "line": child.lineno,
                        "receiver": _source_expression(text, child.func.value),
                        "key": child.args[0].value,
                        "access": "get",
                        "default_supplied": len(child.args) > 1,
                        "default": _source_expression(text, child.args[1]) if len(child.args) > 1 else None,
                    }
                )
        elif isinstance(child, ast.Subscript):
            key = _constant_string(child.slice)
            if key is not None:
                lookups.append(
                    {
                        "line": child.lineno,
                        "receiver": _source_expression(text, child.value),
                        "key": key,
                        "access": "index",
                        "default_supplied": False,
                        "default": None,
                    }
                )
        elif isinstance(child, (ast.If, ast.While, ast.Assert)):
            test = child.test
            conditions.append(
                {
                    "line": child.lineno,
                    "kind": type(child).__name__.lower(),
                    "expression": _source_expression(text, test),
                }
            )
        elif isinstance(child, ast.Return) and child.value is not None:
            returns.append(
                {
                    "line": child.lineno,
                    "expression": _bounded_expression(text, child.value),
                }
            )
    class_attributes: list[dict[str, Any]] = []
    for child in node.body:
        if isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            class_attributes.append(
                {
                    "line": child.lineno,
                    "targets": [_source_expression(text, target) for target in targets],
                    "value": _source_expression(text, child.value),
                }
            )

    return {
        "source": {
            "path": path,
            "start_line": node.lineno,
            "end_line": node.end_lineno,
            "class_sha256": source_sha256,
            "complete_source_in_pack": False,
        },
        "bases": [_source_expression(text, base) for base in node.bases],
        "decorators": [_source_expression(text, decorator) for decorator in node.decorator_list],
        "docstring": ast.get_docstring(node, clean=True),
        "class_attributes": class_attributes,
        "methods": methods,
        "data_lookups": _group_records(
            lookups,
            ("receiver", "key", "access", "default_supplied", "default"),
        ),
        "conditions": _group_records(conditions, ("kind", "expression")),
        "returns": _group_records(returns, ("expression",)),
        "outcomes": _group_records(outcomes, ("callee", "subtest")),
        "direct_dependencies": [
            {
                "callee": callee,
                "lines": sorted(lines),
            }
            for callee, lines in sorted(calls.items())
        ],
        "exceptions_caught": _caught_exceptions(node, text),
        "uncertainties": _deduplicate_records(uncertainties),
    }


def _direct_dependency_sources(
    validation_root: Path,
    interfaces: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Retrieve one bounded source hop for uniquely resolved local helper calls."""

    names = {
        str(dependency["callee"])
        for interface in interfaces.values()
        for dependency in interface.get("direct_dependencies", [])
        if "." not in str(dependency.get("callee", ""))
    }
    if not names:
        return {}

    package_root = validation_root / "isvtest" / "src" / "isvtest"
    matches: dict[str, list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, str]]] = {}
    for path in sorted(package_root.rglob("*.py")) if package_root.is_dir() else ():
        text = _read_text(path)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                matches.setdefault(node.name, []).append((path, node, text))

    sources: dict[str, dict[str, Any]] = {}
    for name, candidates in sorted(matches.items()):
        if len(candidates) != 1:
            continue
        path, node, text = candidates[0]
        source = ast.get_source_segment(text, node)
        if not source:
            continue
        record: dict[str, Any] = {
            "path": str(path.relative_to(validation_root)),
            "start_line": node.lineno,
            "end_line": node.end_lineno,
            "sha256": _sha256_text(source),
            "source_chars": len(source),
        }
        if len(source) <= MAX_DEPENDENCY_SOURCE_CHARS:
            record["source"] = source
        else:
            record["source_omitted"] = "helper exceeds one-hop source bound"
        sources[name] = record
    return sources


def _method_interface(node: ast.FunctionDef | ast.AsyncFunctionDef, text: str) -> dict[str, Any]:
    signature = f"{node.name}({_source_expression(text, node.args)})"
    if node.returns is not None:
        signature += f" -> {_source_expression(text, node.returns)}"
    return {
        "name": node.name,
        "signature": signature,
        "execution_mode": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
        "decorators": [_source_expression(text, decorator) for decorator in node.decorator_list],
        "start_line": node.lineno,
        "end_line": node.end_lineno,
    }


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _is_direct_dependency(callee: str | None) -> bool:
    if callee is None:
        return True
    name = callee.rsplit(".", 1)[-1]
    return (
        name not in BUILTIN_CALLS
        and name not in DYNAMIC_CALLS
        and name not in OUTCOME_CALLS
        and name != "get"
        and not callee.startswith("self.log.")
    )


def _caught_exceptions(node: ast.ClassDef, text: str) -> list[dict[str, Any]]:
    caught: list[dict[str, Any]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.ExceptHandler):
            continue
        caught.append(
            {
                "line": child.lineno,
                "type": _source_expression(text, child.type) if child.type is not None else "BaseException",
            }
        )
    return _deduplicate_records(caught)


def _constant_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _source_expression(text: str, node: ast.AST | None) -> str:
    if node is None:
        return ""
    source = ast.get_source_segment(text, node)
    return source.strip() if source else ast.unparse(node)


def _bounded_expression(text: str, node: ast.AST, *, max_chars: int = 240) -> str | dict[str, Any]:
    expression = _source_expression(text, node)
    if len(expression) <= max_chars:
        return expression
    return {
        "chars": len(expression),
        "sha256": _sha256_text(expression),
        "omitted": "expression exceeds deterministic interface bound",
    }


def _deduplicate_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: (int(item.get("line", 0)), json.dumps(item, sort_keys=True))):
        fingerprint = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(record)
    return result


def _group_records(records: Sequence[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    grouped: dict[str, tuple[dict[str, Any], set[int]]] = {}
    for record in records:
        payload = {field: record.get(field) for field in fields}
        fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        grouped.setdefault(fingerprint, (payload, set()))[1].add(int(record.get("line", 0)))
    return [
        {**payload, "lines": sorted(lines)}
        for _fingerprint, (payload, lines) in sorted(grouped.items())
    ]


def _sync_source(
    project: ReadinessProject,
    manifest_path: Path,
    source: ContextSource,
    *,
    fetcher: Fetcher,
) -> ContextRecord:
    # Network sources are always fetched; an unreachable optional source
    # degrades to a "missing" record instead of blocking the sync.
    is_url = source.location.startswith(("https://", "http://"))
    if source.kind == "web_url" and _is_ncp_guide_source(source.location):
        content = _fetch_ncp_software_reference_guide(fetcher)
        return _record(source, DEFAULT_NSRG_URL, content)

    if source.kind == "web_url" or (source.kind == "api_spec" and is_url):
        raw = fetcher(source.location, {"User-Agent": "isv-readiness-gapctl/0.1"})
        text = _decode_bytes(raw, source.location)
        if _looks_like_html(text):
            text = _html_to_text(text)
        if source.location == DEFAULT_INFERENCE_RA_URL:
            text = _annotate_mermaid_visuals(text)
        return _record(source, source.location, redact_text(text))

    paths = _source_paths(project, manifest_path, source)
    if source.kind == "local_tree":
        content = []
        for root in paths:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(content) >= 50:
                    break
                if _allowed_text_file(path):
                    content.append({"path": str(path), "content": _read_text(path)})
        if not content:
            raise ContextError(f"No readable context files found for {source.id}")
        return _record(source, source.location, content)

    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        raise ContextError(f"Context file not found: {source.location}")
    text = _read_text(path)
    return _record(source, str(path), _decode_json_or_text(text))


def _is_ncp_guide_source(location: str) -> bool:
    return location == DEFAULT_NSRG_URL or location.startswith(NCP_GUIDE_PAGE_PREFIXES)


def _fetch_ncp_software_reference_guide(fetcher: Fetcher) -> str:
    headers = {"User-Agent": "isv-readiness-gapctl/0.1"}
    index = _decode_bytes(fetcher(DEFAULT_NSRG_URL, headers), DEFAULT_NSRG_URL)
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title, url in NCP_GUIDE_LINK_RE.findall(index):
        if not url.startswith(NCP_GUIDE_PAGE_PREFIXES) or url in seen:
            continue
        seen.add(url)
        pages.append((title, url))
    if not pages:
        raise ContextError(f"NCP guide index listed no software-reference pages: {DEFAULT_NSRG_URL}")

    sections = [f"# Complete NCP Software Reference Guide\n\nIndex: {DEFAULT_NSRG_URL}\nPages: {len(pages)}"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(lambda args: fetcher(args[1], headers), pages))
    for (title, url), raw in zip(pages, fetched):
        page = _decode_bytes(raw, url)
        sections.append(f"# {title}\n\nSource: {url}\n\n{redact_text(page)}")
    content = "\n\n---\n\n".join(sections)
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ContextError(f"NCP Software Reference Guide exceeds {MAX_SOURCE_BYTES} bytes")
    return content


def _annotate_mermaid_visuals(markdown: str) -> str:
    """Preserve every Mermaid diagram and append a complete prose rendering.

    The official Inference RA publishes its visuals as Mermaid source. Models
    receive that source unchanged plus deterministic node, group, and
    relationship descriptions, so visual information is not lost in a
    text-only qualification context.
    """

    def annotate(match: re.Match[str]) -> str:
        diagram = match.group(1)
        groups: list[tuple[str, str]] = []
        for line in diagram.splitlines():
            group = MERMAID_SUBGRAPH_RE.match(line)
            if group:
                groups.append(
                    (
                        group.group(1),
                        _plain_mermaid_label(group.group(2) or group.group(3) or group.group(1)),
                    )
                )
        group_aliases = {alias for alias, _label in groups}
        aliases: dict[str, str] = {}
        for node in MERMAID_NODE_RE.finditer(diagram):
            if node.group(1) not in group_aliases:
                aliases[node.group(1)] = _plain_mermaid_label(node.group(2) or node.group(3) or node.group(1))
        relationships = [
            _describe_mermaid_relationship(line.strip(), aliases)
            for line in diagram.splitlines()
            if _is_mermaid_relationship(line)
        ]
        description = [
            "Agent-readable visual description:",
            "Nodes: " + "; ".join(f"{alias} means {label}" for alias, label in aliases.items()) + ".",
        ]
        if groups:
            description.append("Groups: " + "; ".join(label for _alias, label in groups) + ".")
        if relationships:
            description.append("Relationships: " + "; ".join(relationships) + ".")
        return match.group(0) + "\n\n" + "\n".join(description)

    return MERMAID_BLOCK_RE.sub(annotate, markdown)


def _plain_mermaid_label(label: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " — ", label, flags=re.IGNORECASE)).strip()


def _is_mermaid_relationship(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped
        and not stripped.startswith(("%%", "flowchart", "subgraph"))
        and ("--" in stripped or ".->" in stripped)
    )


def _describe_mermaid_relationship(line: str, aliases: Mapping[str, str]) -> str:
    described = line
    for alias in sorted(aliases, key=len, reverse=True):
        described = re.sub(rf"\b{re.escape(alias)}\b", aliases[alias], described)
    described = re.sub(r"\s*<-->\s*", " has a bidirectional connection with ", described)
    described = re.sub(
        r"\s*-\.\s*([^.\n]+?)\s*\.-?>\s*",
        r" has a dotted \1 relationship to ",
        described,
    )
    described = re.sub(r"\s*-->\s*", " leads to ", described)
    return re.sub(r"\s+", " ", described).strip()


def _local_gap_items(
    project: ReadinessProject,
    manifest_path: Path,
    gap: GapRow,
    terms: set[str],
    *,
    whole_domain: bool = False,
    provider_root_override: Path | None = None,
) -> list[ContextItem]:
    items: list[ContextItem] = []
    provider_root = provider_root_override or project.provider_root(manifest_path)
    validation_root = project.validation_root(manifest_path)
    included_paths: set[Path] = set()
    if gap.remediation.target:
        target = _safe_descendant(provider_root, gap.remediation.target)
        if target.is_file():
            included_paths.add(target.resolve())
            items.append(
                _item(
                    "provider_target",
                    "provider_source",
                    "authoritative",
                    target,
                    _read_text(target),
                    "selected gap target",
                )
            )
    if gap.evidence.config_path:
        config = Path(gap.evidence.config_path)
        config = config if config.is_absolute() else provider_root / config
        try:
            config = config.resolve()
            if not any(
                _is_relative_to(config, root)
                for root in (provider_root.resolve(), validation_root.resolve())
            ):
                raise ValueError
        except ValueError:
            config = Path()
        if config.is_file():
            config_text = _read_text(config)
            resolved_config = config.resolve()
            if resolved_config not in included_paths:
                included_paths.add(resolved_config)
                items.append(
                    _item(
                        "provider_config",
                        "provider_config",
                        "authoritative",
                        config,
                        config_text,
                        "complete domain config containing setup, test, teardown, and cross-step data flow",
                    )
                )
            items.extend(
                _provider_lifecycle_source_items(
                    provider_root,
                    config,
                    config_text,
                    gap,
                    included_paths,
                    include_all=whole_domain,
                )
            )
    items.extend(_provider_supporting_source_items(provider_root, included_paths))
    # remediation.aws_reference stays a pointer on the embedded gap row; the
    # reference implementation's contents are deliberately not included
    # (patterns only — generated code must derive from the ISV's own spec).
    return items


def _provider_lifecycle_source_items(
    provider_root: Path,
    config_path: Path,
    config_text: str,
    gap: GapRow,
    included_paths: set[Path],
    *,
    include_all: bool = False,
) -> list[ContextItem]:
    """Include selected, setup, and teardown scripts that define one domain lifecycle."""

    try:
        raw = yaml.safe_load(config_text) or {}
    except yaml.YAMLError:
        return []
    steps = [item for item in _nested_step_mappings(raw) if isinstance(item.get("name"), str)]
    paths: list[tuple[str, str, Path]] = []
    for step in steps:
        name = str(step["name"])
        phase = str(step.get("phase") or "")
        semantic_gap = isinstance(gap.enrichment.get("domain_audit"), dict)
        if (
            not include_all
            and not semantic_gap
            and name != gap.step_name
            and phase not in {"setup", "teardown"}
        ):
            continue
        command = step.get("command")
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        script = next((token for token in tokens if Path(token).suffix.lower() in {".py", ".sh"}), None)
        if script is None:
            continue
        path = (config_path.parent / script).resolve()
        try:
            path.relative_to(provider_root.resolve())
        except ValueError:
            continue
        if path in included_paths or not _allowed_text_file(path):
            continue
        paths.append((phase, name, path))

    items: list[ContextItem] = []
    for phase, name, path in paths:
        included_paths.add(path)
        relative = path.relative_to(provider_root.resolve()).as_posix()
        items.append(
            _item(
                _provider_source_id("provider_lifecycle", relative),
                "provider_source",
                "authoritative",
                path,
                _read_text(path),
                f"existing {phase or 'configured'} lifecycle script for step {name}",
            )
        )
    return items


def _nested_step_mappings(value: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if isinstance(value, dict):
        candidate = value.get("steps")
        if isinstance(candidate, list):
            steps.extend(item for item in candidate if isinstance(item, dict))
        for child in value.values():
            steps.extend(_nested_step_mappings(child))
    elif isinstance(value, list):
        for child in value:
            steps.extend(_nested_step_mappings(child))
    return steps


def _provider_supporting_source_items(
    provider_root: Path, included_paths: set[Path]
) -> list[ContextItem]:
    """Expose current same-domain and provider-shared helpers to later remediation."""

    root = provider_root.resolve()
    common_root = provider_root / "scripts" / "common"
    source_roots = {common_root.resolve()} if common_root.is_dir() else set()
    scripts_root = root / "scripts"
    for path in included_paths:
        try:
            relative = path.relative_to(scripts_root)
        except ValueError:
            continue
        if len(relative.parts) > 1:
            source_roots.add(scripts_root / relative.parts[0])

    items: list[ContextItem] = []
    for source_root in sorted(source_roots):
        prefix = "provider_shared" if source_root == common_root.resolve() else "provider_domain"
        relevance = (
            "existing provider-shared client, transport, naming, polling, or error helper"
            if prefix == "provider_shared"
            else "current same-domain adapter or helper from the evolving provider candidate"
        )
        for path in sorted(source_root.rglob("*")):
            resolved = path.resolve()
            if resolved in included_paths or not _allowed_text_file(resolved):
                continue
            included_paths.add(resolved)
            relative = resolved.relative_to(root).as_posix()
            items.append(
                _item(
                    _provider_source_id(prefix, relative),
                    "provider_source",
                    "authoritative",
                    resolved,
                    _read_text(resolved),
                    relevance,
                )
            )
    return items


def _provider_source_id(prefix: str, relative: str) -> str:
    return prefix + "_" + re.sub(r"[^a-zA-Z0-9_-]+", "_", relative).strip("_")


def _run_items(cache_dir: Path, domain: str, terms: set[str]) -> list[ContextItem]:
    """Empirical evidence from the latest recorded run for a domain.

    Runs live beside the context cache (.gapctl/runs by convention). Observed
    runtime results outrank declared sources, so the pack always carries the
    freshest run's JUnit and log excerpts when they exist.
    """
    run = latest_run(cache_dir.expanduser().resolve().parent / "runs", domain)
    if run is None:
        return []
    items: list[ContextItem] = []
    for label, path in (("junit", run.junit_path), ("log", run.log_path), ("setup", run.setup_json_path)):
        if path is None or not path.is_file():
            continue
        text = _read_capped_tail(path)
        excerpt = _relevant_excerpt(text, terms, 12_000)
        if not excerpt:
            continue
        items.append(
            _item(
                f"latest_run_{domain}_{label}",
                f"run_{label}",
                "empirical",
                path,
                excerpt,
                f"latest {domain} run {run.run_id} {label} evidence",
            )
        )
    return items


def _cached_items(
    records: Sequence[ContextRecord],
    scope: str,
    terms: set[str],
    *,
    preserve_reference: bool = False,
) -> list[ContextItem]:
    items: list[ContextItem] = []
    for record in records:
        if record.status != "synced" or record.content is None:
            continue
        text = (
            record.content if isinstance(record.content, str) else json.dumps(record.content, indent=2, sort_keys=True)
        )
        if record.trust == "authoritative" or (preserve_reference and record.trust == "reference"):
            # Qualification receives authoritative ISV evidence and reference
            # architecture whole. The generator boundary rejects a complete
            # request it cannot carry instead of shortening either source.
            excerpt = text
        else:
            # Guidance earns its budget by matching the selected scope.
            excerpt = _relevant_excerpt(text, terms, 12_000)
            if not excerpt or not _relevance_score(excerpt, terms):
                continue
        items.append(
            _item(
                record.source_id,
                record.kind,
                record.trust,
                record.origin,
                excerpt,
                f"source excerpt matched {scope}, requirement, step, or validation terms",
            )
        )
    return items


def _project_records(project: ReadinessProject, cache_dir: Path) -> tuple[ContextRecord, ...]:
    declared = {source.id for source in project.context_sources}
    return tuple(record for record in load_context_records(cache_dir) if record.source_id in declared)


def _qualify_records(project: ReadinessProject, cache_dir: Path) -> tuple[ContextRecord, ...]:
    records = _project_records(project, cache_dir)
    by_id = {record.source_id: record for record in records}
    unavailable = [
        source.id
        for source in project.context_sources
        if source.required and (source.id not in by_id or by_id[source.id].status != "synced")
    ]
    if unavailable:
        raise ContextError("Required qualification context is not synced: " + ", ".join(unavailable))
    return records


def _source_paths(project: ReadinessProject, manifest_path: Path, source: ContextSource) -> tuple[Path, ...]:
    value = Path(source.location).expanduser()
    if value.is_absolute():
        return (value.resolve(),)
    base = manifest_path.resolve().parent
    return (
        (base / value).resolve(),
        (project.provider_root(manifest_path) / value).resolve(),
        (project.validation_root(manifest_path) / value).resolve(),
    )


def _safe_descendant(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ContextError(f"Context target escapes provider root: {relative}") from exc
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _allowed_text_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink() or path.name.lower() in SENSITIVE_FILENAMES:
        return False
    if any(part in {".git", ".venv", "node_modules"} for part in path.parts):
        return False
    return path.suffix.lower() in TEXT_EXTENSIONS and path.stat().st_size <= MAX_SOURCE_BYTES


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ContextError(f"Context file exceeds {MAX_SOURCE_BYTES} bytes: {path}")
    return redact_text(raw.decode("utf-8"))


def _read_capped_tail(path: Path) -> str:
    """Read the end of a run artifact, tolerating oversized or dirty logs.

    The tail is the informative part of a test log (failure summaries print
    last), and run artifacts are machine output, so decode errors are replaced
    rather than fatal.
    """
    raw = path.read_bytes()[-MAX_SOURCE_BYTES:]
    return redact_text(raw.decode("utf-8", errors="replace"))


def _decode_bytes(raw: bytes, origin: str) -> str:
    if len(raw) > MAX_SOURCE_BYTES:
        raise ContextError(f"Fetched context exceeds {MAX_SOURCE_BYTES} bytes: {origin}")
    return raw.decode("utf-8")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:256].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<head" in head


def _html_to_text(html: str) -> str:
    """Extract visible prose from a fetched HTML page.

    Rendered documentation sites are almost entirely markup and script by
    weight; only the visible text is guidance. Cache that, whole, instead of
    the page source.
    """
    parser = _VisibleTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    text = " ".join(parser.chunks)
    return re.sub(r"\s+", " ", text).strip() or html


class _VisibleTextParser(HTMLParser):
    _SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def redact_text(text: str) -> str:
    return redact_failure_text(text)


def _decode_json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _record(source: ContextSource, origin: str, content: Any) -> ContextRecord:
    content = _redact_content(content)
    serialized = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    return ContextRecord(source.id, source.kind, source.trust, origin, "synced", _sha256_text(serialized), content)


def _write_record(cache_dir: Path, record: ContextRecord) -> None:
    path = cache_dir / f"{record.source_id}.json"
    path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _item(source_id: str, kind: str, trust: str, origin: Path | str, content: str, relevance: str) -> ContextItem:
    return ContextItem(source_id, kind, trust, str(origin), _sha256_text(content), content, False, relevance)


def _gap_terms(gap: GapRow) -> set[str]:
    raw = " ".join(
        value
        for value in (
            gap.domain,
            gap.step_name,
            gap.validation_class,
            gap.requirement_id,
            *gap.labels,
            gap.evidence.message,
        )
        if value
    )
    return _tokenize(raw)


def _tokenize(raw: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).lower()
    return {token for token in re.findall(r"[a-z0-9_-]+", expanded) if len(token) >= 3}


def _relevance_score(text: str, terms: set[str]) -> int:
    lowered = text.lower()
    return sum(min(lowered.count(term), 3) for term in terms)


def _relevant_excerpt(text: str, terms: set[str], limit: int) -> str:
    if len(text) <= limit:
        return text
    blocks = [block.strip() for block in re.split(r"\n\s*\n|(?<=})\n|(?<=:)\n", text) if block.strip()]
    ranked = sorted(
        ((_relevance_score(block, terms), index, block) for index, block in enumerate(blocks)),
        key=lambda item: (-item[0], item[1]),
    )
    selected: list[tuple[int, str]] = []
    used = 0
    for score, index, block in ranked:
        if score == 0 and selected:
            continue
        remaining = limit - used
        if remaining <= 0:
            break
        selected.append((index, block[:remaining]))
        used += min(len(block), remaining)
    return "\n\n".join(block for _, block in sorted(selected))


def _trust_rank(trust: str) -> int:
    # Empirical run artifacts outrank every declared source: an observed
    # runtime result overrides what any spec or doc claims.
    return {"empirical": 0, "authoritative": 1, "reference": 2, "advisory": 3}.get(trust, 4)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _redact_content(value: Any) -> Any:
    return redact_failure_value(value)


def _fetch_url(url: str, headers: Mapping[str, str]) -> bytes:
    last_exc: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            request = urllib.request.Request(url, headers=dict(headers))
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(MAX_SOURCE_BYTES + 1)
        except urllib.error.URLError as exc:
            last_exc = exc
    raise ContextError(f"Failed to fetch {url} after 3 attempts: {last_exc}") from last_exc


def _unexpected_cache_check_fetch(url: str, headers: Mapping[str, str]) -> bytes:
    """Prevent a cache freshness check from performing network I/O."""

    del headers
    raise ContextError(f"Cache freshness check attempted an unexpected network fetch: {url}")


def _gap_from_dict(raw: Mapping[str, Any]) -> GapRow:
    return GapRow(
        id=str(raw["id"]),
        domain=str(raw["domain"]),
        step_name=str(raw["step_name"]),
        validation_class=raw.get("validation_class"),
        requirement_id=raw.get("requirement_id"),
        status=raw["status"],
        detection=raw["detection"],
        stage=raw["stage"],
        evidence=Evidence(**raw["evidence"]),
        remediation=Remediation(**raw["remediation"]),
        enrichment=dict(raw.get("enrichment") or {}),
        labels=tuple(label for label in raw.get("labels", []) if isinstance(label, str)),
    )
