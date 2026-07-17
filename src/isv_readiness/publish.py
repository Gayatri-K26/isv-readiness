"""Publish a validation bundle to ISV Lab Service using stdlib only."""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PublishError(ValueError):
    """Raised when publishing to ISV Lab Service cannot proceed safely."""


_DOMAIN_TO_PLATFORM = {
    "k8s": "KUBERNETES",
    "kubernetes": "KUBERNETES",
    "slurm": "SLURM",
    "bare_metal": "BARE_METAL",
    "vm": "VM",
    "network": "VM",
    "iam": "VM",
    "control-plane": "VM",
    "image-registry": "VM",
    "observability": "VM",
    "security": "VM",
}


def _detect_platform(domains: list[str]) -> str:
    for domain in domains:
        if domain in _DOMAIN_TO_PLATFORM:
            return _DOMAIN_TO_PLATFORM[domain]
    return "VM"


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
            return json.loads(resp.read().decode())["access_token"]
    except HTTPError as exc:
        raise PublishError(
            f"Failed to obtain JWT token (HTTP {exc.code}). Check ISV_CLIENT_ID and ISV_CLIENT_SECRET."
        ) from exc
    except URLError as exc:
        raise PublishError(f"Could not reach SSA issuer ({ssa_issuer}): {exc.reason}") from exc
    except KeyError as exc:
        raise PublishError("JWT response did not contain an access_token field.") from exc


def publish_bundle(
    bundle_dir: Path,
    *,
    lab_id: int,
    junit_xml_path: Path | None = None,
    platform: str | None = None,
    isv_software_version: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Publish a gapctl validation bundle to ISV Lab Service.

    Raises:
        PublishError: If the bundle or credentials are missing/invalid
    """
    bundle_dir = bundle_dir.expanduser().resolve()
    manifest_path = bundle_dir / "bundle-manifest.json"
    if not manifest_path.is_file():
        raise PublishError(
            f"bundle-manifest.json not found in {bundle_dir}. "
            "Run `gapctl bundle` first to assemble the validation evidence."
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provider = manifest.get("provider", "unknown")
    outcome = manifest.get("outcome", "incomplete")
    domains = [d["domain"] for d in manifest.get("domains", [])]

    resolved_platform = platform or _detect_platform(domains)
    resolved_tags = list(tags or []) + [provider] + domains

    endpoint = _require_env("ISV_SERVICE_ENDPOINT")
    ssa_issuer = _require_env("ISV_SSA_ISSUER")
    client_id = _require_env("ISV_CLIENT_ID")
    client_secret = _require_env("ISV_CLIENT_SECRET")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    print(f"Provider:  {provider}")
    print(f"Domains:   {', '.join(domains)}")
    print(f"Outcome:   {outcome}")
    print(f"Platform:  {resolved_platform}")
    print(f"Lab ID:    {lab_id}")
    print()

    jwt_token = _get_jwt_token(ssa_issuer, client_id, client_secret)

    payload: dict = {
        "executedBy": "gapctl",
        "ciReference": f"gapctl bundle {bundle_dir.name}",
        "tags": resolved_tags,
        "testTargetType": resolved_platform,
        "testRunStartAt": now,
    }
    if isv_software_version:
        payload["isvSoftwareVersion"] = isv_software_version

    result = _post(f"{endpoint}/v1/labs/{lab_id}/test-runs", payload, token=jwt_token)
    test_run_id = result["data"]["testRunId"]
    print(f"Test run created: {test_run_id}")

    if junit_xml_path is not None:
        junit_path = junit_xml_path.expanduser().resolve()
        if not junit_path.is_file():
            raise PublishError(f"JUnit XML not found: {junit_path}")
        print(f"Uploading JUnit XML: {junit_path}")
        _post(
            f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}/test-results",
            {"junitXml": junit_path.read_text(encoding="utf-8")},
            token=jwt_token,
        )

    complete_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    status = "SUCCESS" if outcome == "validation_complete" else "FAILED"
    update_payload: dict = {"testRunStatus": status, "testRunCompleteAt": complete_time}
    if isv_software_version:
        update_payload["isvSoftwareVersion"] = isv_software_version
    _post(
        f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}",
        update_payload,
        token=jwt_token,
        method="PUT",
    )
    print(f"Status:    {status}")

    url = f"{endpoint}/v1/labs/{lab_id}/test-runs/{test_run_id}"
    print()
    print(f"Results published: {url}")
    return url


def check_publish_credentials() -> list[str]:
    """Return a list of missing credential env var names."""
    required = ["ISV_SERVICE_ENDPOINT", "ISV_SSA_ISSUER", "ISV_CLIENT_ID", "ISV_CLIENT_SECRET"]
    return [name for name in required if not os.environ.get(name)]
