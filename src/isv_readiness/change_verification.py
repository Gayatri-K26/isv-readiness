from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import jsonschema

from isv_readiness.changes import (
    ChangeProposal,
    ChangeSet,
    ProposalFile,
    build_change_proposal,
    resolve_change_target,
)
from isv_readiness.fixes import FixGuardrailError, select_gap
from isv_readiness.scan.scanner import ScanOptions, scan_provider
from isv_readiness.verification import VerificationError, find_regressions

CHANGE_VERIFICATION_VERSION = "0.1.0"
CHANGE_APPLICATION_VERSION = "0.1.0"


@dataclass(frozen=True)
class ChangeVerificationManifest:
    schema_version: str
    verification_mode: str
    gap_id: str
    domain: str
    change_set_sha256: str
    patch_sha256: str
    files: tuple[ProposalFile, ...]
    selected_status_before: str
    selected_status_after: str | None
    regressions: tuple[str, ...]
    success: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        payload["regressions"] = list(self.regressions)
        return payload


@dataclass(frozen=True)
class AppliedFile:
    target_root: str
    path: str
    before_sha256: str | None
    after_sha256: str
    backup_path: str | None


@dataclass(frozen=True)
class ChangeApplicationResult:
    schema_version: str
    gap_id: str
    change_set_sha256: str
    patch_sha256: str
    files: tuple[AppliedFile, ...]
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        return payload


def verify_change_set(
    report: dict[str, Any],
    *,
    provider_repo: Path,
    change_set: ChangeSet,
    validation_root: Path | None,
) -> ChangeVerificationManifest:
    selected = select_gap(report, change_set.gap_id)
    if selected.get("detection") != "static":
        raise VerificationError(
            "Isolated change-set verification supports static gaps only; dynamic gaps require a reviewed live rerun."
        )
    proposal = build_change_proposal(report, provider_repo=provider_repo, change_set=change_set)
    provider_root = provider_repo.resolve()
    with tempfile.TemporaryDirectory(prefix="gapctl-change-verify-") as tempdir:
        workspace = Path(tempdir) / "workspace"
        workspace.mkdir()
        isolated_provider = workspace / provider_root.name
        shutil.copytree(
            provider_root,
            isolated_provider,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"),
        )
        _copy_wrappers(provider_root, isolated_provider)
        for change in change_set.changes:
            target = resolve_change_target(isolated_provider, change, domain=proposal.domain)
            target.write_text(change.content, encoding="utf-8")
            source = resolve_change_target(provider_root, change, domain=proposal.domain)
            mode = stat.S_IMODE(source.stat().st_mode) if source.exists() else _new_file_mode(target)
            target.chmod(mode)
        rescanned = scan_provider(
            ScanOptions(
                provider_repo=isolated_provider,
                domains=[proposal.domain],
                validation_root=validation_root,
            )
        ).to_dict()

    selected_after = next((row for row in rescanned["rows"] if row.get("id") == change_set.gap_id), None)
    status_after = selected_after.get("status") if selected_after else None
    regressions = find_regressions(
        report,
        rescanned,
        domain=proposal.domain,
        selected_gap_id=change_set.gap_id,
    )
    manifest = ChangeVerificationManifest(
        schema_version=CHANGE_VERIFICATION_VERSION,
        verification_mode="isolated_static_change_set_rescan",
        gap_id=change_set.gap_id,
        domain=proposal.domain,
        change_set_sha256=proposal.change_set_sha256,
        patch_sha256=proposal.patch_sha256,
        files=proposal.files,
        selected_status_before=str(selected.get("status")),
        selected_status_after=str(status_after) if status_after is not None else None,
        regressions=tuple(regressions),
        success=status_after == "pass" and not regressions,
    )
    _validate_schema(manifest.to_dict(), "change-verification.schema.json")
    return manifest


def load_change_verification(path: Path) -> ChangeVerificationManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw, "change-verification.schema.json")
    return ChangeVerificationManifest(
        schema_version=raw["schema_version"],
        verification_mode=raw["verification_mode"],
        gap_id=raw["gap_id"],
        domain=raw["domain"],
        change_set_sha256=raw["change_set_sha256"],
        patch_sha256=raw["patch_sha256"],
        files=tuple(ProposalFile(**item) for item in raw["files"]),
        selected_status_before=raw["selected_status_before"],
        selected_status_after=raw["selected_status_after"],
        regressions=tuple(raw["regressions"]),
        success=raw["success"],
    )


def apply_verified_change_set(
    report: dict[str, Any],
    *,
    provider_repo: Path,
    change_set: ChangeSet,
    manifest: ChangeVerificationManifest,
    backup_dir: Path,
) -> ChangeApplicationResult:
    if not manifest.success or manifest.regressions:
        raise VerificationError("Change-set verification is not successful or contains regressions.")
    proposal = build_change_proposal(report, provider_repo=provider_repo, change_set=change_set)
    _match_manifest(proposal, manifest)
    provider_root = provider_repo.resolve()
    file_by_key = {(item.target_root, item.path): item for item in proposal.files}
    targets: list[tuple[Any, Path, ProposalFile]] = []
    for change in change_set.changes:
        target = resolve_change_target(provider_root, change, domain=proposal.domain)
        file = file_by_key[(change.target_root, change.path)]
        current = _file_sha256(target) if target.exists() else None
        if current != file.before_sha256:
            raise VerificationError(f"Provider target changed after verification: {change.path}")
        if not target.parent.is_dir():
            raise VerificationError(f"Target parent directory does not exist: {target.parent}")
        targets.append((change, target, file))

    backup_root = backup_dir.resolve() / f"{change_set.gap_id}-{proposal.patch_sha256[:12]}"
    if backup_root.exists():
        raise VerificationError(f"Backup transaction already exists: {backup_root}")
    backups: dict[Path, Path] = {}
    staged: dict[Path, Path] = {}
    applied: list[Path] = []
    try:
        for change, target, _ in targets:
            if target.exists():
                backup = backup_root / change.target_root / f"{change.path}.bak"
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[target] = backup
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else _new_file_mode(target)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
                staged_path = Path(handle.name)
                handle.write(change.content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            staged_path.chmod(mode)
            staged[target] = staged_path

        for _, target, _ in targets:
            os.replace(staged.pop(target), target)
            applied.append(target)

        results = []
        for change, target, file in targets:
            after = _file_sha256(target)
            if after != file.after_sha256:
                raise VerificationError(f"Applied target hash does not match verified content: {change.path}")
            results.append(
                AppliedFile(
                    target_root=change.target_root,
                    path=change.path,
                    before_sha256=file.before_sha256,
                    after_sha256=after,
                    backup_path=str(backups[target]) if target in backups else None,
                )
            )
    except Exception as exc:
        rollback_errors = _rollback(applied, backups)
        if rollback_errors:
            raise VerificationError(
                f"Change-set application failed and rollback was incomplete: {exc}; {'; '.join(rollback_errors)}"
            ) from exc
        if isinstance(exc, (VerificationError, FixGuardrailError)):
            raise
        raise VerificationError(f"Change-set application failed and was rolled back: {exc}") from exc
    finally:
        for staged_path in staged.values():
            if staged_path.exists():
                staged_path.unlink()

    result = ChangeApplicationResult(
        schema_version=CHANGE_APPLICATION_VERSION,
        gap_id=change_set.gap_id,
        change_set_sha256=proposal.change_set_sha256,
        patch_sha256=proposal.patch_sha256,
        files=tuple(results),
        applied=True,
    )
    _validate_schema(result.to_dict(), "change-application.schema.json")
    return result


def _match_manifest(proposal: ChangeProposal, manifest: ChangeVerificationManifest) -> None:
    if manifest.gap_id != proposal.gap_id:
        raise VerificationError("Verification gap does not match the requested change set.")
    if manifest.change_set_sha256 != proposal.change_set_sha256 or manifest.patch_sha256 != proposal.patch_sha256:
        raise VerificationError("Change set or provider source changed after verification.")
    if manifest.files != proposal.files:
        raise VerificationError("Verified file hashes do not match the current proposal.")


def _copy_wrappers(provider_root: Path, isolated_provider: Path) -> None:
    for suffix in (".yaml", ".yml"):
        source = provider_root.with_suffix(suffix)
        if source.is_file():
            shutil.copy2(source, isolated_provider.with_suffix(suffix))


def _new_file_mode(path: Path) -> int:
    return 0o755 if path.suffix.lower() == ".sh" else 0o644


def _rollback(applied: list[Path], backups: dict[Path, Path]) -> list[str]:
    errors = []
    for target in reversed(applied):
        try:
            backup = backups.get(target)
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(backup, target)
        except OSError as exc:
            errors.append(f"{target}: {exc}")
    return errors


def _validate_schema(raw: Any, name: str) -> None:
    path = Path(__file__).resolve().parents[2] / "schemas" / name
    schema = json.loads(path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path) or name
        raise VerificationError(f"Invalid {name} at {location}: {exc.message}") from exc


def _file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
