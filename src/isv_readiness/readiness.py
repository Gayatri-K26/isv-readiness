from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from isv_readiness.decision import blocking_rows, validation_profile_issues
from isv_readiness.project import ReadinessProject
from isv_readiness.runs import RunRecord, latest_run
from isv_readiness.scan.models import SCHEMA_VERSION
from isv_readiness.solution_profile import load_solution_profile


@dataclass(frozen=True)
class DomainEvidence:
    domain: str
    run: RunRecord
    junit_xml: str


@dataclass(frozen=True)
class ReadinessAssessment:
    blocking_count: int
    profile_issues: tuple[str, ...]
    report_issues: tuple[str, ...]
    unvalidated_domains: tuple[str, ...]
    evidence_issues: tuple[str, ...]
    evidence: tuple[DomainEvidence, ...]

    @property
    def ready(self) -> bool:
        return not (
            self.blocking_count
            or self.profile_issues
            or self.report_issues
            or self.unvalidated_domains
            or self.evidence_issues
        )


def assess_readiness(
    project: ReadinessProject,
    project_path: Path,
    report: dict,
) -> ReadinessAssessment:
    """Evaluate the local evidence required by both status and publication."""

    report_issues: list[str] = []
    expected_domains = set(project.assessment.domains)
    reported_domains = set(report.get("domains", [])) if isinstance(report.get("domains"), list) else set()
    rows = report.get("rows")
    if report.get("schema_version") != SCHEMA_VERSION:
        report_issues.append(f"gaps.json must use schema {SCHEMA_VERSION}")
    if reported_domains != expected_domains:
        report_issues.append("gaps.json must cover the complete project scope")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        rows = []
        report_issues.append("gaps.json rows are invalid")

    profile = (
        load_solution_profile(project.resolve_path(project_path, project.assessment.profile))
        if project.assessment.profile
        else None
    )
    profile_issues = tuple(validation_profile_issues(profile, project.assessment.domains))
    blocking_count = len(blocking_rows(report)) if not report_issues else 0

    runs_root = project_path.parent / ".gapctl" / "runs"
    evidence: list[DomainEvidence] = []
    unvalidated: list[str] = []
    evidence_issues: list[str] = []
    for domain in project.assessment.domains:
        has_dynamic_pass = any(
            row.get("domain") == domain
            and row.get("detection") == "dynamic"
            and row.get("status") == "pass"
            for row in rows
        )
        record = latest_run(runs_root, domain)
        if not has_dynamic_pass or record is None or record.exit_code != 0:
            unvalidated.append(domain)
            continue
        junit_path = record.junit_path
        if junit_path is None:
            evidence_issues.append(f"latest {domain} run {record.run_id} has no JUnit XML")
            continue
        try:
            junit_xml = junit_path.read_text(encoding="utf-8")
            ElementTree.fromstring(junit_xml)
        except (OSError, UnicodeDecodeError, ElementTree.ParseError):
            evidence_issues.append(f"latest {domain} run {record.run_id} has invalid JUnit XML")
            continue
        evidence.append(DomainEvidence(domain=domain, run=record, junit_xml=junit_xml))

    return ReadinessAssessment(
        blocking_count=blocking_count,
        profile_issues=profile_issues,
        report_issues=tuple(report_issues),
        unvalidated_domains=tuple(unvalidated),
        evidence_issues=tuple(evidence_issues),
        evidence=tuple(evidence),
    )
