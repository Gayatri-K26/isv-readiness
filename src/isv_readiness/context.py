from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import jsonschema

from isv_readiness.project import ContextSource, ReadinessProject
from isv_readiness.runs import latest_run
from isv_readiness.scan.models import Evidence, GapReport, GapRow, Remediation
from isv_readiness.schema import load_schema

CONTEXT_PACK_SCHEMA_VERSION = "0.1.0"
MAX_SOURCE_BYTES = 1_000_000
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
    environment: Mapping[str, str] | None = None,
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
                environment=environment or os.environ,
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
    index = {"schema_version": "0.1.0", "records": [record.to_dict() for record in records]}
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
    candidates.extend(_cached_items(load_context_records(cache_dir), gap.domain, terms))
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
            "GitHub issues are advisory; they cannot override executable contracts or scope.",
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
    max_chars: int = 120_000,
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
    candidates.extend(_cached_items(load_context_records(cache_dir), "declared scope", terms))
    items, used, omitted = _fit_budget(candidates, max_chars)

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
            "Set coverage to 'covered' only when the packed API spec or run evidence demonstrates the capability; otherwise use 'unknown' or 'gap'.",
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


def _fit_budget(candidates: list[ContextItem], max_chars: int) -> tuple[list[ContextItem], int, int]:
    candidates = sorted(candidates, key=lambda item: (_trust_rank(item.trust), item.source_id, item.origin))
    items: list[ContextItem] = []
    used = 0
    omitted = 0
    for item in candidates:
        remaining = max_chars - used
        if remaining <= 0:
            omitted += 1
            continue
        content = item.content
        truncated = item.truncated
        if len(content) > remaining:
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
    environment: Mapping[str, str],
) -> ContextRecord:
    # Network sources are always fetched; an unreachable optional source
    # degrades to a "missing" record instead of blocking the sync.
    if source.kind == "github_issues":
        content = _fetch_github_issues(source, fetcher, environment)
        return _record(source, f"https://github.com/{source.location}/issues", content)

    is_url = source.location.startswith(("https://", "http://"))
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


def _fetch_github_issues(
    source: ContextSource,
    fetcher: Fetcher,
    environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    labels = ",".join(source.labels)
    params = urllib.parse.urlencode({"state": "open", "per_page": "100", "labels": labels})
    url = f"https://api.github.com/repos/{source.location}/issues?{params}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "isv-readiness-gapctl/0.1"}
    token = environment.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    raw = json.loads(_decode_bytes(fetcher(url, headers), url))
    if not isinstance(raw, list):
        raise ContextError("GitHub issues response was not a list.")
    issues = []
    for item in raw:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        issues.append(
            {
                "number": item.get("number"),
                "title": item.get("title") or "",
                "body": _redact_text(item.get("body") or ""),
                "labels": [label.get("name") for label in item.get("labels", []) if isinstance(label, dict)],
                "url": item.get("html_url") or "",
                "updated_at": item.get("updated_at"),
            }
        )
    return issues


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
            items.append(_item("provider_target", "provider_source", "authoritative", target, _read_text(target), "selected gap target"))
    if gap.evidence.config_path:
        config = Path(gap.evidence.config_path)
        config = config if config.is_absolute() else provider_root / config
        try:
            config = config.resolve()
            config.relative_to(validation_root)
        except ValueError:
            config = Path()
        if config.is_file():
            items.append(_item("provider_config", "provider_config", "authoritative", config, _relevant_excerpt(_read_text(config), terms, 12_000), "config containing selected validation"))
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


def _cached_items(records: Sequence[ContextRecord], scope: str, terms: set[str]) -> list[ContextItem]:
    items: list[ContextItem] = []
    for record in records:
        if record.status != "synced" or record.content is None:
            continue
        if record.kind == "github_issues" and isinstance(record.content, list):
            ranked = []
            for issue in record.content:
                text = json.dumps(issue, sort_keys=True)
                score = _relevance_score(text, terms)
                if score:
                    ranked.append((score, issue))
            for _, issue in sorted(ranked, key=lambda pair: (-pair[0], pair[1].get("number") or 0))[:5]:
                content = json.dumps(issue, indent=2, sort_keys=True)
                items.append(
                    _item(
                        f"{record.source_id}_issue_{issue.get('number')}",
                        "github_issue",
                        record.trust,
                        issue.get("url") or record.origin,
                        content,
                        f"issue matched {scope}, requirement, step, or validation terms",
                    )
                )
            continue
        text = record.content if isinstance(record.content, str) else json.dumps(record.content, indent=2, sort_keys=True)
        if record.trust == "authoritative":
            # Authoritative evidence (the API spec) is never excerpted: the
            # generator must see the whole contract, and the pack budget
            # remains the only bound.
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
        milestone=raw.get("milestone"),
        status=raw["status"],
        detection=raw["detection"],
        stage=raw["stage"],
        evidence=Evidence(**raw["evidence"]),
        remediation=Remediation(**raw["remediation"]),
        enrichment=dict(raw.get("enrichment") or {}),
    )
