from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_report(report: dict[str, Any], fmt: str) -> str:
    if fmt == "scorecard":
        return render_scorecard(report)
    if fmt == "tree":
        return render_tree(report)
    if fmt == "md":
        return render_markdown(report)
    raise ValueError(f"Unsupported report format: {fmt}")


def render_scorecard(report: dict[str, Any]) -> str:
    rows = report.get("rows") or []
    counts = Counter(row.get("status") for row in rows)
    by_domain: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_domain[row.get("domain", "<unknown>")][row.get("status", "<unknown>")] += 1

    total = len(rows)
    active = total - counts.get("skipped", 0)
    pass_count = counts.get("pass", 0)
    score = 100.0 if active == 0 else (pass_count / active) * 100.0
    score_label = "Readiness score" if any(row.get("detection") == "dynamic" for row in rows) else "Static score"
    lines = [
        "Gap Scorecard",
        f"Schema: {report.get('schema_version', '<unknown>')}",
        f"Provider repo: {report.get('provider_repo', '<unknown>')}",
        f"{score_label}: {score:.1f}% ({pass_count}/{active} non-skipped rows pass)",
        "Statuses: " + _counter_text(counts),
        "",
        "By domain:",
    ]
    for domain in sorted(by_domain):
        lines.append(f"- {domain}: {_counter_text(by_domain[domain])}")
    return "\n".join(lines)


def render_tree(report: dict[str, Any]) -> str:
    rows = report.get("rows") or []
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row.get("domain", "<unknown>")][row.get("step_name", "<unknown>")].append(row)

    lines = ["Gap Tree"]
    for domain in sorted(grouped):
        lines.append(domain)
        for step_name in sorted(grouped[domain]):
            lines.append(f"  {step_name}")
            for row in sorted(grouped[domain][step_name], key=lambda item: item.get("validation_class") or ""):
                validation = row.get("validation_class") or "<none>"
                lines.append(f"    [{row.get('status')}] {validation} ({row.get('id')})")
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    rows = report.get("rows") or []
    lines = [
        "# Gap Report",
        "",
        f"- Schema: `{report.get('schema_version', '<unknown>')}`",
        f"- Provider repo: `{report.get('provider_repo', '<unknown>')}`",
        "",
        "| ID | Domain | Step | Validation | Status | Stage | Gap Type | Target |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        remediation = row.get("remediation") or {}
        lines.append(
            "| {id} | {domain} | {step} | {validation} | {status} | {stage} | {gap_type} | {target} |".format(
                id=_cell(row.get("id")),
                domain=_cell(row.get("domain")),
                step=_cell(row.get("step_name")),
                validation=_cell(row.get("validation_class")),
                status=_cell(row.get("status")),
                stage=_cell(row.get("stage")),
                gap_type=_cell(row.get("gap_type")),
                target=_cell(remediation.get("target")),
            )
        )
    return "\n".join(lines)


def _counter_text(counter: Counter[str]) -> str:
    keys = ["pass", "fail", "not_implemented", "skipped", "error"]
    parts = [f"{key}={counter.get(key, 0)}" for key in keys if counter.get(key, 0)]
    return ", ".join(parts) if parts else "none"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|")
