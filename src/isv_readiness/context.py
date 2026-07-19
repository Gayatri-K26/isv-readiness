from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import jsonschema

from isv_readiness.project import DEFAULT_NSRG_URL, ContextSource, ReadinessProject
from isv_readiness.runs import latest_run
from isv_readiness.scan.models import Evidence, GapReport, GapRow, Remediation
from isv_readiness.schema import load_schema

CONTEXT_PACK_SCHEMA_VERSION = "0.1.0"
CONTEXT_CACHE_SCHEMA_VERSION = "0.2.0"
MAX_SOURCE_BYTES = 1_000_000
NCP_GUIDE_PAGE_PREFIXES = (
    "https://docs.nvidia.com/dsx/ncp/software-reference-guide/",
    "https://docs.nvidia.com/dsx/ncp/part-1-software-reference-guide/",
    "https://docs.nvidia.com/dsx/ncp/part-2-software-components/",
)
NCP_GUIDE_LINK_RE = re.compile(r"\[([^\]]+)\]\((https://docs\.nvidia\.com/dsx/ncp/[^)]+\.md)\)")
QUALIFICATION_MAPPING_RULES = (
    "Match capabilities explicitly declared by the ISV's supplied interfaces and "
    "documentation to the closest applicable checks in each declared domain.",
    "Match the required behavior, not nearby terminology: a read or inventory interface "
    "does not prove create, update, placement, retention, aggregation, policy, or other "
    "semantics that the evidence does not declare.",
    "Every validation class or step grouped under one capability selector must be "
    "independently supported by the cited evidence; split the group or leave a check "
    "unmatched when its behavior differs.",
    "Treat each catalog requirement as its step and validation-class pair. A class-only "
    "selector matches that class in every step; use one only when the evidence supports "
    "every occurrence, otherwise constrain the selector by both steps and classes.",
    "Treat an API specification as authoritative evidence of the interfaces the ISV "
    "declares, not proof that those interfaces work in the target environment.",
    "Use covered/test when a declared capability maps to an upstream check and should "
    "be validated; runtime results, not qualification claims, determine whether it passes.",
    "Do not use unknown/deferred merely because the target version, credentials, hardware, "
    "topology, or runtime behavior are unverified when supplied ISV evidence explicitly "
    "declares the capability; map it to covered/test and let validation determine the result.",
    "Use covered/test as a domain default only when the supplied ISV evidence explicitly "
    "maps every check in that domain's pinned catalog.",
    "For a partially supported domain, add grouped covered/test capability entries for "
    "explicitly mapped checks; do not use a covered domain default to fill unmapped checks.",
    "Use out_of_scope/skip as a partial-domain default only when the supplied evidence "
    "explicitly excludes every unmatched check from the product claim; otherwise use "
    "unknown/deferred so an SME must decide.",
    "Use out_of_scope/skip only for a product-scope exclusion, never merely because the "
    "current lab lacks hardware, credentials, or runtime evidence.",
    "Missing provider script implementations are validate-phase gaps, not product "
    "capability gaps.",
    "Do not assign numeric nsrg_layers unless a supplied source explicitly maps the "
    "component to those exact layer numbers.",
)
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
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*[:=]\s*)([^\n]+)$"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)

Fetcher = Callable[[str, Mapping[str, str]], bytes]


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
    index = {"schema_version": CONTEXT_CACHE_SCHEMA_VERSION, "records": [record.to_dict() for record in records]}
    (cache_dir / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tuple(records)


def load_context_records(cache_dir: Path) -> tuple[ContextRecord, ...]:
    records: list[ContextRecord] = []
    if not cache_dir.exists():
        return ()
    for path in sorted(cache_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        records.append(ContextRecord(**raw))
    return tuple(records)


def context_cache_is_current(project: ReadinessProject, cache_dir: Path) -> bool:
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
    cached_ids = {record.get("source_id") for record in records if isinstance(record, dict)}
    expected_ids = {source.id for source in project.context_sources}
    return cached_ids == expected_ids and all((cache_dir / f"{source_id}.json").is_file() for source_id in expected_ids)


def build_context_pack(
    project: ReadinessProject,
    manifest_path: Path,
    report: GapReport | dict[str, Any],
    *,
    gap_id: str,
    cache_dir: Path,
    environment: Mapping[str, str] | None = None,
    max_chars: int = 48_000,
    feedback: Sequence[str] = (),
) -> ContextPack:
    if max_chars < 4_000:
        raise ContextError("Context budget must be at least 4000 characters.")
    rows = report.rows if isinstance(report, GapReport) else [_gap_from_dict(row) for row in report.get("rows", [])]
    gap = next((row for row in rows if row.id == gap_id), None)
    if gap is None:
        raise ContextError(f"Gap not found: {gap_id}")
    if gap.domain not in project.assessment.domains:
        raise ContextError(f"Gap domain '{gap.domain}' is outside the selected project scope.")

    env = environment or os.environ
    required_env = sorted(set(project.execution.credential_env))
    available_env = sorted(name for name in required_env if env.get(name))
    terms = _gap_terms(gap)
    candidates = _local_gap_items(project, manifest_path, gap, terms)
    if feedback:
        candidates.append(
            _item(
                "previous_attempt_feedback",
                "verifier_feedback",
                "authoritative",
                "gapctl://previous-attempt",
                "\n\n".join(feedback),
                "feedback from the prior static or live verification attempt",
            )
        )
    candidates.extend(_run_items(cache_dir, gap.domain, terms))
    candidates.extend(_cached_items(_project_records(project, cache_dir), gap.domain, terms))
    items, used, omitted = _fit_budget(candidates, max_chars)

    pack = ContextPack(
        schema_version=CONTEXT_PACK_SCHEMA_VERSION,
        project={
            "provider": project.provider.name,
            "owned_domains": list(project.assessment.domains),
            "validation_commit": project.validation.resolved_commit,
            "api_interfaces": [
                {
                    "id": api.id,
                    "kind": api.kind,
                    "base_url": api.base_url,
                    "base_url_env": api.base_url_env,
                    "spec": api.spec,
                    "auth_env": list(api.auth_env),
                    "domains": list(api.domains),
                }
                for api in project.apis
                if gap.domain in api.domains
            ],
        },
        gap=gap.to_dict(),
        credentials={"required_env": required_env, "available_env": available_env},
        constraints=(
            "Treat ai-cloud-validation suites and validation classes as read-only source-of-truth contracts.",
            "Change only provider-owned files authorized by the selected scope and change-set policy.",
            "Never place credential values in source, patches, prompts, reports, or logs.",
            "Prior-run artifacts are empirical evidence of runtime behavior; when they conflict with declared sources or profile claims, trust the run results.",
            "Results attest only to the ISV-owned scope; do not claim coverage of domains or layers the ISV does not own.",
        ),
        items=tuple(items),
        budget={"max_chars": max_chars, "used_chars": used, "omitted_items": omitted},
    )
    validate_context_pack(pack.to_dict())
    return pack


def build_qualify_pack(
    project: ReadinessProject,
    catalog: Mapping[str, Any],
    *,
    cache_dir: Path,
    max_chars: int = 300_000,
) -> dict[str, Any]:
    """Build the profile-scoped evidence pack for qualify-phase drafting.

    Unlike the per-gap pack this covers every declared domain at once: the
    suite catalog (what NVIDIA demands), the cached API spec and guidance
    (what the ISV exposes), and the latest recorded run per domain.
    """
    if max_chars < 4_000:
        raise ContextError("Context budget must be at least 4000 characters.")
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
    items, used, omitted = _fit_budget(candidates, max_chars, allow_truncation=False)

    pack = {
        "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
        "purpose": "qualify_draft",
        "project": {
            "provider": project.provider.name,
            "declared_domains": sorted(domains),
            "validation_commit": project.validation.resolved_commit,
            "api_interfaces": [
                {
                    "id": api.id,
                    "kind": api.kind,
                    "base_url": api.base_url,
                    "base_url_env": api.base_url_env,
                    "spec": api.spec,
                    "auth_env": list(api.auth_env),
                    "domains": list(api.domains),
                }
                for api in project.apis
            ],
        },
        "constraints": [
            "Treat ai-cloud-validation suites and validation classes as read-only source-of-truth contracts.",
            *QUALIFICATION_MAPPING_RULES,
            "Use the NCP Software Reference Guide to interpret capabilities and architecture only; it cannot expand ISV ownership or override the pinned validation contracts.",
            "Ownership fields are suggestions for SME review; never add domains or invent scope beyond the declared domains.",
            "Prior-run artifacts are empirical evidence of runtime behavior; when they conflict with declared sources or profile claims, trust the run results.",
            "Never place credential values in source, patches, prompts, reports, or logs.",
        ],
        "items": [_jsonable(asdict(item)) for item in items],
        "budget": {"max_chars": max_chars, "used_chars": used, "omitted_items": omitted},
    }
    _validate_against_schema(pack, "qualify-pack.schema.json", "qualify pack")
    return pack


def validate_context_pack(raw: Any) -> None:
    _validate_against_schema(raw, "context-pack.schema.json", "context pack")


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
                    f"Qualification context exceeds {max_chars} characters before source '{item.source_id}'; "
                    "refusing to omit evidence."
                )
            omitted += 1
            continue
        content = item.content
        truncated = item.truncated
        if len(content) > remaining:
            if not allow_truncation:
                raise ContextError(
                    f"Qualification context exceeds {max_chars} characters at source '{item.source_id}'; "
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
        return _record(source, source.location, _redact_text(text))

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
    for title, url in pages:
        page = _decode_bytes(fetcher(url, headers), url)
        sections.append(f"# {title}\n\nSource: {url}\n\n{_redact_text(page)}")
    content = "\n\n---\n\n".join(sections)
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ContextError(f"NCP Software Reference Guide exceeds {MAX_SOURCE_BYTES} bytes")
    return content


def _local_gap_items(
    project: ReadinessProject,
    manifest_path: Path,
    gap: GapRow,
    terms: set[str],
) -> list[ContextItem]:
    items: list[ContextItem] = []
    provider_root = project.provider_root(manifest_path)
    validation_root = project.validation_root(manifest_path)
    if gap.remediation.target:
        target = _safe_descendant(provider_root, gap.remediation.target)
        if target.is_file():
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
            config.relative_to(validation_root)
        except ValueError:
            config = Path()
        if config.is_file():
            items.append(
                _item(
                    "provider_config",
                    "provider_config",
                    "authoritative",
                    config,
                    _relevant_excerpt(_read_text(config), terms, 12_000),
                    "config containing selected validation",
                )
            )
    # remediation.aws_reference stays a pointer on the embedded gap row; the
    # reference implementation's contents are deliberately not included
    # (patterns only — generated code must derive from the ISV's own spec).
    return items


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
            # architecture whole. Its budget fails closed instead of silently
            # shortening either source.
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
    return _redact_text(raw.decode("utf-8"))


def _read_capped_tail(path: Path) -> str:
    """Read the end of a run artifact, tolerating oversized or dirty logs.

    The tail is the informative part of a test log (failure summaries print
    last), and run artifacts are machine output, so decode errors are replaced
    rather than fatal.
    """
    raw = path.read_bytes()[-MAX_SOURCE_BYTES:]
    return _redact_text(raw.decode("utf-8", errors="replace"))


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


def _redact_text(text: str) -> str:
    text = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    return AWS_KEY_RE.sub("[REDACTED AWS KEY]", text)


def redact_text(text: str) -> str:
    """Redact common credential forms before persisting external command output."""
    return _redact_text(text)


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
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_content(item) for item in value]
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            name = str(key)
            if any(marker in name.upper() for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")):
                redacted[name] = "[REDACTED]"
            else:
                redacted[name] = _redact_content(item)
        return redacted
    return value


def _fetch_url(url: str, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(MAX_SOURCE_BYTES + 1)


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
