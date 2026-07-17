"""Organized prior-run artifacts.

Every ``scan --run`` invocation records its artifacts under
``<runs-root>/<run-id>/`` with canonical names (``junit.xml``,
``isvctl.log``, ``setup.json``) plus a ``run.json`` metadata record.
Run ids start with a UTC timestamp so lexicographic order is
chronological order, which keeps "latest run" resolution deterministic
without parsing dates. The context packer treats the latest run per
domain as empirical evidence that outranks every declared source.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RUN_RECORD_FILENAME = "run.json"
JUNIT_FILENAME = "junit.xml"
LOG_FILENAME = "isvctl.log"
SETUP_FILENAME = "setup.json"
_RUN_ID_SAFE_RE = re.compile(r"[^a-z0-9-]+")


class RunRecordError(ValueError):
    """Raised when a run record cannot be created or written."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    domain: str
    created_at: str
    config: str | None
    exit_code: int | None
    path: Path

    @property
    def junit_path(self) -> Path | None:
        return _existing(self.path / JUNIT_FILENAME)

    @property
    def log_path(self) -> Path | None:
        return _existing(self.path / LOG_FILENAME)

    @property
    def setup_json_path(self) -> Path | None:
        return _existing(self.path / SETUP_FILENAME)


def new_run_dir(runs_root: Path, domain: str, *, created_at: str | None = None) -> tuple[str, Path]:
    """Create and return the next run directory for a domain."""
    stamp = created_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_domain = _RUN_ID_SAFE_RE.sub("-", domain.lower().replace("_", "-")).strip("-")
    if not safe_domain:
        raise RunRecordError(f"Domain does not yield a usable run id: {domain!r}")
    run_id = f"{stamp}-{safe_domain}"
    run_dir = runs_root / run_id
    suffix = 2
    while run_dir.exists():
        run_dir = runs_root / f"{run_id}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)
    return run_dir.name, run_dir


def write_run_record(
    run_dir: Path,
    *,
    run_id: str,
    domain: str,
    config: str | None,
    exit_code: int | None,
    created_at: str | None = None,
) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        domain=domain,
        created_at=created_at or datetime.now(UTC).isoformat(timespec="seconds"),
        config=config,
        exit_code=exit_code,
        path=run_dir,
    )
    payload = {
        "run_id": record.run_id,
        "domain": record.domain,
        "created_at": record.created_at,
        "config": record.config,
        "exit_code": record.exit_code,
        "artifacts": sorted(
            path.name
            for path in (record.junit_path, record.log_path, record.setup_json_path)
            if path is not None
        ),
    }
    (run_dir / RUN_RECORD_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return record


def latest_run(runs_root: Path, domain: str) -> RunRecord | None:
    """Resolve the newest recorded run for a domain, or None."""
    if not runs_root.is_dir():
        return None
    best: RunRecord | None = None
    for run_dir in sorted(runs_root.iterdir()):
        record_path = run_dir / RUN_RECORD_FILENAME
        if not record_path.is_file():
            continue
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or raw.get("domain") != domain:
            continue
        record = RunRecord(
            run_id=str(raw.get("run_id") or run_dir.name),
            domain=domain,
            created_at=str(raw.get("created_at") or ""),
            config=raw.get("config"),
            exit_code=raw.get("exit_code"),
            path=run_dir,
        )
        if best is None or record.run_id >= best.run_id:
            best = record
    return best


def _existing(path: Path) -> Path | None:
    return path if path.is_file() else None
