from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from isv_readiness.scan.k8s_onboard import (
    PROVIDER_NAME_RE,
    K8sOnboardingPlan,
    _write_text,
    build_k8s_onboarding_plan,
    write_k8s_onboarding_files,
)
from isv_readiness.solution_profile import SUPPORTED_DOMAINS, SolutionProfile, canonicalize_domain

DOMAIN_CONFIG_FILES = {
    "bare_metal": "bare_metal.yaml",
    "control_plane": "control-plane.yaml",
    "iam": "iam.yaml",
    "image_registry": "image-registry.yaml",
    "network": "network.yaml",
    "observability": "observability.yaml",
    "security": "security.yaml",
    "slurm": "slurm.yaml",
    "vm": "vm.yaml",
}

DOMAIN_REQUIRED_INPUTS = {
    "bare_metal": (
        "provisioning API or CLI specification and authentication",
        "test hardware inventory and allowed lifecycle operations",
        "SSH access, image, storage, fabric, and sanitization ownership",
    ),
    "control_plane": (
        "tenant control-plane API specification and test tenant",
        "access-key, tenant, quota, and object-storage lifecycle ownership",
    ),
    "iam": (
        "identity API and authentication flow",
        "test users, roles, credentials, MFA, and OIDC policy",
    ),
    "image_registry": (
        "registry API, credentials, and test image artifacts",
        "VM and bare-metal image/install-configuration integration",
    ),
    "kubernetes": (
        "kubectl command or kubeconfig/context",
        "cluster, node-pool, GPU operator, CNI, CSI, identity, and workload ownership",
        "NGC credentials when NIM or GPU workload checks are in scope",
    ),
    "network": (
        "SDN API and authentication",
        "VPC, subnet, security-group, IPAM, fabric, and tenant-isolation ownership",
    ),
    "observability": (
        "log, metric, event, and telemetry query interfaces",
        "retention windows and access-control policy",
    ),
    "security": (
        "security architecture and shared-responsibility matrix",
        "MFA, OIDC, KMS, audit, attestation, and tenant-isolation evidence sources",
    ),
    "slurm": (
        "Slurm controller access and test partition",
        "GPU scheduling, container runtime, and workload policy",
    ),
    "vm": (
        "VM lifecycle API and authentication",
        "test image, network, storage, SSH, GPU, and sanitization policy",
    ),
}

CommandRunner = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[str]]


class OnboardingError(RuntimeError):
    """Raised when provider onboarding cannot be planned or completed safely."""


@dataclass(frozen=True)
class ProviderOnboardingPlan:
    provider_name: str
    validation_root: Path
    provider_dir: Path
    domains: tuple[str, ...]
    scaffold_command: tuple[str, ...]
    required_inputs: dict[str, tuple[str, ...]]
    k8s_plan: K8sOnboardingPlan | None

    def summary_lines(self) -> list[str]:
        lines = [
            f"Provider: {self.provider_name}",
            f"Target: {self.provider_dir}",
            "Domains: " + ", ".join(self.domains),
            "Scaffold command: " + " ".join(self.scaffold_command),
            "The upstream scaffold creates the complete provider template; selected domains drive readiness work.",
        ]
        if self.k8s_plan is not None:
            lines.append(f"Kubernetes wrapper completion: {self.k8s_plan.wrapper_path}")
        if "slurm" in self.domains:
            lines.append(
                f"Slurm wrapper completion: {self.provider_dir / 'config' / DOMAIN_CONFIG_FILES['slurm']}"
            )
        lines.append("Required ISV inputs:")
        for domain in self.domains:
            lines.append(f"[{domain}]")
            lines.extend(f"- {item}" for item in self.required_inputs[domain])
        return lines


def build_provider_onboarding_plan(
    validation_root: Path,
    provider_name: str,
    domains: Sequence[str],
    *,
    profile: SolutionProfile | None = None,
) -> ProviderOnboardingPlan:
    if not PROVIDER_NAME_RE.fullmatch(provider_name):
        raise OnboardingError("Provider name must contain only lowercase letters, numbers, '_' and '-'.")
    validation_root = validation_root.resolve()
    template_dir = validation_root / "isvctl" / "configs" / "providers" / "my-isv"
    if not template_dir.is_dir():
        raise OnboardingError(f"Provider template not found: {template_dir}")

    normalized_domains = tuple(dict.fromkeys(canonicalize_domain(domain) for domain in domains))
    if not normalized_domains:
        raise OnboardingError("At least one onboarding domain is required.")
    unsupported = sorted(set(normalized_domains).difference(SUPPORTED_DOMAINS))
    if unsupported:
        raise OnboardingError(f"Unsupported onboarding domains: {', '.join(unsupported)}")

    command_prefix = _isvctl_command_prefix(validation_root)
    provider_dir = template_dir.parent / provider_name
    required_inputs = {
        domain: _inputs_for_domain(domain, profile)
        for domain in normalized_domains
    }
    k8s_plan = (
        build_k8s_onboarding_plan(validation_root, provider_name)
        if "kubernetes" in normalized_domains
        else None
    )
    return ProviderOnboardingPlan(
        provider_name=provider_name,
        validation_root=validation_root,
        provider_dir=provider_dir,
        domains=normalized_domains,
        scaffold_command=(*command_prefix, "provider", "scaffold", provider_name),
        required_inputs=required_inputs,
        k8s_plan=k8s_plan,
    )


def execute_provider_onboarding(
    plan: ProviderOnboardingPlan,
    *,
    overwrite: bool = False,
    runner: CommandRunner | None = None,
    timeout_seconds: int = 300,
) -> list[Path]:
    command = [*plan.scaffold_command]
    if overwrite:
        command.append("--overwrite")
    run = runner or _default_runner
    result = run(command, plan.validation_root, timeout_seconds)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        raise OnboardingError(
            f"Provider scaffold failed with exit code {result.returncode}: {details or 'no output'}"
        )

    written = [
        plan.provider_dir / "config" / DOMAIN_CONFIG_FILES[domain]
        for domain in plan.domains
        if domain in DOMAIN_CONFIG_FILES
        and (plan.provider_dir / "config" / DOMAIN_CONFIG_FILES[domain]).exists()
    ]
    if plan.k8s_plan is not None:
        written.extend(
            write_k8s_onboarding_files(
                plan.k8s_plan,
                overwrite=overwrite,
                preserve_existing_scripts=True,
            )
        )
    if "slurm" in plan.domains:
        # Slurm is a unified suite like Kubernetes: the upstream scaffold ships
        # scripts/slurm/ but no per-provider config, so onboarding supplies the
        # wrapper. An existing hand-authored config is preserved.
        slurm_config = plan.provider_dir / "config" / DOMAIN_CONFIG_FILES["slurm"]
        if overwrite or not slurm_config.exists():
            _write_text(slurm_config, _slurm_wrapper_text(plan.provider_name), overwrite=True)
            written.append(slurm_config)
    return list(dict.fromkeys(written))


def _slurm_wrapper_text(provider_name: str) -> str:
    return f"""import:
  - ../../../suites/slurm.yaml

version: "1.0"

commands:
  slurm:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: setup
        phase: setup
        command: "../scripts/slurm/setup.sh"
        timeout: 120
      - name: teardown
        phase: teardown
        command: "../scripts/slurm/teardown.sh"
        timeout: 30

tests:
  description: "{provider_name} Slurm validation"
  platform: slurm
"""


def _inputs_for_domain(domain: str, profile: SolutionProfile | None) -> tuple[str, ...]:
    if profile is None:
        return DOMAIN_REQUIRED_INPUTS[domain]
    profile_domain = next((item for item in profile.domains if item.domain == domain), None)
    if profile_domain is None:
        return DOMAIN_REQUIRED_INPUTS[domain]
    values = [*profile_domain.required_inputs]
    for capability in profile_domain.capabilities:
        values.extend(capability.required_inputs)
    return tuple(dict.fromkeys(values)) or DOMAIN_REQUIRED_INPUTS[domain]


def _isvctl_command_prefix(validation_root: Path) -> tuple[str, ...]:
    local_executable = validation_root / ".venv" / "bin" / "isvctl"
    if local_executable.is_file():
        return (str(local_executable),)
    return ("uv", "run", "isvctl")


def _default_runner(
    command: Sequence[str], cwd: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
