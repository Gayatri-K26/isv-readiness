"""Publish canonical recorded validation evidence to ISV Lab Service."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from isv_readiness.project import ProjectError, load_project
from isv_readiness.readiness import assess_readiness
from isv_readiness.scan.report import load_report
from isv_readiness.solution_profile import SolutionProfileError


class PublishError(ValueError):
    """Raised when publishing to ISV Lab Service cannot proceed safely."""


_DOMAIN_TO_PLATFORM = {
    "k8s": "KUBERNETES",
    "kubernetes": "KUBERNETES",
    "slurm": "SLURM",
    "bare_metal": "BARE_METAL",
    "vm": "VM",
    "network": "NETWORK",
    "iam": "IAM",
    "control_plane": "CONTROL_PLANE",
    "image_registry": "IMAGE_REGISTRY",
    "observability": "OBSERVABILITY",
    "security": "SECURITY",
}
CommitResolver = Callable[[Path], str]


def _platform_for_domain(domain: str) -> str:
    try:
        return _DOMAIN_TO_PLATFORM[domain]
    except KeyError as exc:
        raise PublishError(f"Cannot infer a publish platform for domain: {domain}") from exc


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise PublishError(
            f"Missing required environment variable: {name}\n"
            "Set ISV_SERVICE_ENDPOINT, ISV_SSA_ISSUER, ISV_CLIENT_ID, and ISV_CLIENT_SECRET before publishing."
        )
    return value


def _post(url: str, payload: dict, *, token: str, method: str = "POST") -> dict:
    body = json.dumps(payload).encode()
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method=method,
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode()[:1000]
        raise PublishError(f"ISV Lab Service returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise PublishError(f"Could not reach ISV Lab Service ({url}): {exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"ISV Lab Service returned an invalid JSON response: {url}") from exc


def _get_jwt_token(ssa_issuer: str, client_id: str, client_secret: str) -> str:
    token_url = f"{ssa_issuer}/token"
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = b"scope=create-isv-lab-test-run update-isv-lab-test-run&grant_type=client_credentials"
    req = Request(
        token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            response = json.loads(resp.read().decode())
            return response["access_token"]
    except HTTPError as exc:
        raise PublishError(
            f"Failed to obtain JWT token (HTTP {exc.code}). Check ISV_CLIENT_ID and ISV_CLIENT_SECRET."
        ) from exc
    except URLError as exc:
        raise PublishError(f"Could not reach SSA issuer ({ssa_issuer}): {exc.reason}") from exc
    except KeyError as exc:
        raise PublishError("JWT response did not contain an access_token field.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("JWT endpoint returned an invalid JSON response.") from exc


def publish_project(
    project_path: Path,
    *,
    lab_id: int,
    isv_software_version: str | None = None,
    tags: list[str] | None = None,
    commit_resolver: CommitResolver | None = None,
) -> tuple[str, ...]:
    """Publish the latest successful recorded run for every owned domain.

    Raises:
        PublishError: If local evidence or publication credentials are invalid.
    """
    project_path = project_path.expanduser().resolve()
    if lab_id < 1:
        raise PublishError("Lab ID must be a positive integer.")
    try:
        project = load_project(project_path)
        report_path = project_path.parent / "gaps.json"
        if not report_path.is_file():
            raise PublishError("gaps.json not found; run gapctl test and gapctl status before publishing.")
        report = load_report(report_path)
        readiness = assess_readiness(project, project_path, report)
    except PublishError:
        raise
    except (OSError, json.JSONDecodeError, ProjectError, SolutionProfileError) as exc:
        raise PublishError(f"Could not validate local publication evidence: {exc}") from exc

    if not readiness.ready:
        issues = list(readiness.profile_issues)
        if readiness.blocking_count:
            issues.append(f"{readiness.blocking_count} blocking gap(s) remain")
        issues.extend(readiness.report_issues)
        issues.extend(f"live validation required for {domain}" for domain in readiness.unvalidated_domains)
        issues.extend(readiness.evidence_issues)
        raise PublishError("Project is not ready to publish: " + "; ".join(issues))

    expected_commit = project.validation.resolved_commit
    if expected_commit is None:
        raise PublishError("Project does not pin an ai-cloud-validation commit.")
    current_commit = (commit_resolver or _resolve_commit)(project.validation_root(project_path))
    if current_commit != expected_commit:
        raise PublishError(
            f"Validation checkout drifted from pinned commit {expected_commit} to {current_commit}; rerun qualification."
        )

    endpoint = _require_env("ISV_SERVICE_ENDPOINT").rstrip("/")
    ssa_issuer = _require_env("ISV_SSA_ISSUER").rstrip("/")
    client_id = _require_env("ISV_CLIENT_ID")
    client_secret = _require_env("ISV_CLIENT_SECRET")

    jwt_token = _get_jwt_token(ssa_issuer, client_id, client_secret)
    provider = project.provider.name
    urls: list[str] = []
    for evidence in readiness.evidence:
        platform = _platform_for_domain(evidence.domain)
        resolved_tags = list(dict.fromkeys([*list(tags or []), provider, evidence.domain]))
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        payload: dict = {
            "executedBy": "gapctl",
            "ciReference": f"gapctl test {evidence.domain} ({evidence.run.run_id})",
            "tags": resolved_tags,
            "testTargetType": platform,
            "testRunStartAt": now,
        }
        if isv_software_version:
            payload["isvSoftwareVersion"] = isv_software_version

        print(f"Provider:  {provider}")
        print(f"Domain:    {evidence.domain}")
        print(f"Run:       {evidence.run.run_id}")
        print(f"Platform:  {platform}")
        print(f"Lab ID:    {lab_id}")
        print()

        result = _post(f"{endpoint}/v1/labs/{lab_id}/test-runs", payload, token=jwt_token)
        try:
            test_run_id = str(result["data"]["testRunId"])
        except (KeyError, TypeError) as exc:
            raise PublishError("ISV Lab Service create response did not contain data.testRunId.") from exc
        print(f"Test run created: {test_run_id}")
        print(f"Uploading JUnit XML: {evidence.run.junit_path}")
        _post(
            f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}/test-results",
            {"junitXml": evidence.junit_xml},
            token=jwt_token,
        )
        complete_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        update_payload: dict = {"testRunStatus": "SUCCESS", "testRunCompleteAt": complete_time}
        if isv_software_version:
            update_payload["isvSoftwareVersion"] = isv_software_version
        _post(
            f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}",
            update_payload,
            token=jwt_token,
            method="PUT",
        )
        print("Status:    SUCCESS")

        url = f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}"
        urls.append(url)
        print()
        print(f"Results published: {url}")
    return tuple(urls)


def _resolve_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    commit = (result.stdout or "").strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PublishError(f"Could not resolve validation commit: {(result.stderr or '').strip()}")
    return commit
