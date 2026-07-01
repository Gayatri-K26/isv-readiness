from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from isv_readiness.fixes import FixGuardrailError, build_fix_proposal, select_gap
from isv_readiness.scan.scanner import ScanOptions, scan_provider

VERIFICATION_MANIFEST_VERSION = "0.1.0"
APPLICATION_RESULT_VERSION = "0.1.0"
UNRESOLVED_STATUSES = {"fail", "not_implemented", "error"}


class VerificationError(ValueError):
    """Raised when verification or controlled application cannot proceed safely."""


@dataclass(frozen=True)
class VerificationManifest:
    schema_version: str
    verification_mode: str
    gap_id: str
    domain: str
    target: str
    patch_sha256: str
    candidate_sha256: str
    target_before_sha256: str | None
    selected_status_before: str
    selected_status_after: str | None
    regressions: tuple[str, ...]
    success: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["regressions"] = list(self.regressions)
        return payload


@dataclass(frozen=True)
class ApplicationResult:
    schema_version: str
    gap_id: str
    target: str
    patch_sha256: str
    target_before_sha256: str | None
    target_after_sha256: str
    backup_path: str | None
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_fix_candidate(
    report: dict[str, Any],
    *,
    gap_id: str,
    provider_repo: Path,
    candidate_path: Path,
    validation_root: Path | None,
) -> VerificationManifest:
    selected = select_gap(report, gap_id)
    if selected.get("detection") != "static":
        raise VerificationError(
            "The isolated verifier currently supports static gaps only; dynamic gaps require a reviewed live rerun."
        )
    domain = selected.get("domain")
    if not isinstance(domain, str) or not domain:
        raise VerificationError(f"Gap {gap_id} has no valid domain.")

    proposal = build_fix_proposal(
        report,
        gap_id=gap_id,
        provider_repo=provider_repo,
        candidate_path=candidate_path,
    )
    provider_root = provider_repo.resolve()
    if not provider_root.is_dir():
        raise VerificationError(f"Provider repository is not a directory: {provider_root}")

    candidate_bytes = candidate_path.read_bytes()
    target = provider_root / proposal.target
    target_before_sha256 = _file_sha256(target) if target.exists() else None

    with tempfile.TemporaryDirectory(prefix="gapctl-verify-") as tempdir:
        workspace = Path(tempdir) / "workspace"
        workspace.mkdir()
        isolated_provider = workspace / provider_root.name
        shutil.copytree(
            provider_root,
            isolated_provider,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
        )
        _copy_top_level_wrapper(provider_root, isolated_provider)

        isolated_target = isolated_provider / proposal.target
        isolated_target.parent.mkdir(parents=True, exist_ok=True)
        isolated_target.write_bytes(candidate_bytes)
        if target.exists():
            isolated_target.chmod(stat.S_IMODE(target.stat().st_mode))
        else:
            isolated_target.chmod(stat.S_IMODE(candidate_path.stat().st_mode))

        rescanned = scan_provider(
            ScanOptions(
                provider_repo=isolated_provider,
                domains=[domain],
                validation_root=validation_root,
            )
        ).to_dict()

    selected_after = next((row for row in rescanned["rows"] if row.get("id") == gap_id), None)
    selected_status_after = selected_after.get("status") if selected_after else None
    regressions = find_regressions(report, rescanned, domain=domain, selected_gap_id=gap_id)
    success = selected_status_after == "pass" and not regressions
    return VerificationManifest(
        schema_version=VERIFICATION_MANIFEST_VERSION,
        verification_mode="isolated_static_rescan",
        gap_id=gap_id,
        domain=domain,
        target=proposal.target,
        patch_sha256=proposal.patch_sha256,
        candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        target_before_sha256=target_before_sha256,
        selected_status_before=str(selected.get("status")),
        selected_status_after=str(selected_status_after) if selected_status_after is not None else None,
        regressions=tuple(regressions),
        success=success,
    )


def load_verification_manifest(path: Path) -> VerificationManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != VERIFICATION_MANIFEST_VERSION:
        raise VerificationError(
            f"Unsupported verification manifest version: {payload.get('schema_version', '<missing>')}"
        )
    regressions = payload.get("regressions") or []
    if not isinstance(regressions, list) or not all(isinstance(item, str) for item in regressions):
        raise VerificationError("Verification regressions must be a list of strings.")
    try:
        return VerificationManifest(
            schema_version=payload["schema_version"],
            verification_mode=payload["verification_mode"],
            gap_id=payload["gap_id"],
            domain=payload["domain"],
            target=payload["target"],
            patch_sha256=payload["patch_sha256"],
            candidate_sha256=payload["candidate_sha256"],
            target_before_sha256=payload.get("target_before_sha256"),
            selected_status_before=payload["selected_status_before"],
            selected_status_after=payload.get("selected_status_after"),
            regressions=tuple(regressions),
            success=payload["success"],
        )
    except KeyError as exc:
        raise VerificationError(f"Verification manifest is missing field: {exc.args[0]}") from exc


def apply_verified_candidate(
    report: dict[str, Any],
    *,
    gap_id: str,
    provider_repo: Path,
    candidate_path: Path,
    manifest: VerificationManifest,
    backup_dir: Path,
) -> ApplicationResult:
    if manifest.success is not True or manifest.regressions:
        raise VerificationError("Verification manifest is not successful or contains regressions.")
    if manifest.gap_id != gap_id:
        raise VerificationError(
            f"Verification manifest gap '{manifest.gap_id}' does not match requested gap '{gap_id}'."
        )

    try:
        proposal = build_fix_proposal(
            report,
            gap_id=gap_id,
            provider_repo=provider_repo,
            candidate_path=candidate_path,
        )
    except FixGuardrailError as exc:
        raise VerificationError(str(exc)) from exc
    if proposal.target != manifest.target or proposal.patch_sha256 != manifest.patch_sha256:
        raise VerificationError("Current candidate/source no longer matches the verified patch.")

    candidate_bytes = candidate_path.read_bytes()
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    if candidate_sha256 != manifest.candidate_sha256:
        raise VerificationError("Candidate content changed after verification.")

    provider_root = provider_repo.resolve()
    target = provider_root / proposal.target
    current_sha256 = _file_sha256(target) if target.exists() else None
    if current_sha256 != manifest.target_before_sha256:
        raise VerificationError("Provider target changed after verification; generate and verify a new proposal.")
    if not target.parent.is_dir():
        raise VerificationError(f"Target parent directory does not exist: {target.parent}")

    backup_path: Path | None = None
    if target.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{gap_id}-{proposal.patch_sha256[:12]}-{target.name}.bak"
        if backup_path.exists():
            raise VerificationError(f"Backup already exists: {backup_path}")
        shutil.copy2(target, backup_path)

    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else stat.S_IMODE(candidate_path.stat().st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(candidate_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    target_after_sha256 = _file_sha256(target)
    if target_after_sha256 != candidate_sha256:
        raise VerificationError("Applied target hash does not match the verified candidate hash.")
    return ApplicationResult(
        schema_version=APPLICATION_RESULT_VERSION,
        gap_id=gap_id,
        target=proposal.target,
        patch_sha256=proposal.patch_sha256,
        target_before_sha256=current_sha256,
        target_after_sha256=target_after_sha256,
        backup_path=str(backup_path) if backup_path else None,
        applied=True,
    )


def _copy_top_level_wrapper(provider_root: Path, isolated_provider: Path) -> None:
    for suffix in (".yaml", ".yml"):
        wrapper = provider_root.with_suffix(suffix)
        if wrapper.is_file():
            shutil.copy2(wrapper, isolated_provider.with_suffix(suffix))


def find_regressions(
    baseline: dict[str, Any],
    rescanned: dict[str, Any],
    *,
    domain: str,
    selected_gap_id: str,
) -> list[str]:
    before = {
        str(row.get("id")): row
        for row in baseline.get("rows", [])
        if row.get("domain") == domain and row.get("detection") == "static"
    }
    after = {str(row.get("id")): row for row in rescanned.get("rows", []) if row.get("domain") == domain}
    regressions: list[str] = []
    for gap_id, row in before.items():
        if gap_id == selected_gap_id:
            continue
        updated = after.get(gap_id)
        if row.get("status") == "pass" and (updated is None or updated.get("status") != "pass"):
            regressions.append(f"{gap_id}: prior pass no longer passes")
    for gap_id, row in after.items():
        if gap_id != selected_gap_id and gap_id not in before and row.get("status") in UNRESOLVED_STATUSES:
            regressions.append(f"{gap_id}: new unresolved {row.get('status')} row")
    return sorted(regressions)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
