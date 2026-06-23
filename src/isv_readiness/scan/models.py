from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "0.1.0"

Status = Literal["pass", "fail", "not_implemented", "skipped", "error"]
Detection = Literal["static", "dynamic"]
Stage = Literal["coverage", "correctness"]
GapType = Literal[
    "not_implemented",
    "provider_script",
    "product_bug",
    "lab_env",
    "semantic_mismatch",
    "lib_adoption",
    "onboarding",
]


@dataclass(frozen=True)
class Evidence:
    message: str
    validation_message: str | None = None
    schema_errors: list[str] = field(default_factory=list)
    missing_json_fields: list[str] = field(default_factory=list)
    stderr_excerpt: str | None = None
    script_path: str | None = None
    config_path: str | None = None


@dataclass(frozen=True)
class Remediation:
    auto_fixable: bool
    target: str | None
    rerun_command: str
    aws_reference: str | None = None


@dataclass(frozen=True)
class GapRow:
    id: str
    domain: str
    step_name: str
    validation_class: str | None
    requirement_id: str | None
    milestone: str | None
    status: Status
    detection: Detection
    stage: Stage
    gap_type: GapType
    evidence: Evidence
    remediation: Remediation
    enrichment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IsvContext:
    repo_access: str = "local"
    api_spec: str | None = None
    run_env: str = "not_checked"
    creds_scope: str | None = None


@dataclass(frozen=True)
class GapReport:
    schema_version: str
    provider_repo: str
    domains: list[str]
    isv_context: IsvContext
    rows: list[GapRow]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rows"] = [row.to_dict() for row in self.rows]
        return data
