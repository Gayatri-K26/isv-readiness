from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "0.2.0"

Status = Literal["pass", "fail", "not_implemented", "skipped", "error"]
Detection = Literal["static", "dynamic", "semantic"]
Stage = Literal["coverage", "correctness"]


@dataclass(frozen=True)
# Why a check fired
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
    # Whether policy permits a generator to propose a guarded candidate edit.
    auto_fixable: bool  # profile can flip it off but never on
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
    status: Status
    detection: Detection
    stage: Stage
    evidence: Evidence
    remediation: Remediation
    enrichment: dict[str, Any] = field(default_factory=dict)
    labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["labels"] = list(self.labels)
        return data


@dataclass(frozen=True)
class GapReport:
    schema_version: str
    provider_repo: str
    domains: list[str]
    rows: list[GapRow]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rows"] = [row.to_dict() for row in self.rows]
        return data
