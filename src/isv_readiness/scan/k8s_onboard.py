from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

K8S_ISV_INFO_FIELDS = [
    "provider_name",
    "kubectl command or kubeconfig/context",
    "whether the ISV owns cluster lifecycle or only validates an existing cluster",
    "owned K8s layers: node inventory, node pools, GPU operator, NetworkPolicy, CSI, identity, observability, workloads",
    "expected node and GPU counts, if known",
    "GPU runtimeClass and GPU resource name, if customized",
    "GPU operator namespace and driver/toolkit ownership",
    "StorageClass names when CSI is in scope",
    "NetworkPolicy support and CNI plugin details",
    "NGC_API_KEY availability for NIM/workload validations",
    "provider API docs or repo access when setup scripts need TODO implementation",
]

# Missing ownership means unknown. Only an explicit SME/ISV answer may turn a
# layer into in-scope (true) or out-of-scope (false).
SCOPE_TEMPLATE_OWNS: dict[str, bool] = {}


@dataclass(frozen=True)
class K8sOnboardingPlan:
    provider_name: str
    validation_root: Path
    providers_dir: Path
    provider_dir: Path
    wrapper_path: Path
    setup_script_path: Path
    teardown_script_path: Path
    scope_template_path: Path
    template_setup_script: Path
    template_teardown_script: Path

    def summary_lines(self) -> list[str]:
        return [
            f"Provider: {self.provider_name}",
            f"Wrapper: {self.wrapper_path}",
            f"Setup script: {self.setup_script_path}",
            f"Teardown script: {self.teardown_script_path}",
            f"Scope template: {self.scope_template_path}",
            "Required ISV info:",
            *[f"- {field}" for field in K8S_ISV_INFO_FIELDS],
        ]


def build_k8s_onboarding_plan(validation_root: Path, provider_name: str) -> K8sOnboardingPlan:
    provider_name = _validate_provider_name(provider_name)
    validation_root = validation_root.resolve()
    providers_dir = validation_root / "isvctl" / "configs" / "providers"
    provider_dir = providers_dir / provider_name
    template_dir = providers_dir / "my-isv" / "scripts" / "k8s"
    return K8sOnboardingPlan(
        provider_name=provider_name,
        validation_root=validation_root,
        providers_dir=providers_dir,
        provider_dir=provider_dir,
        wrapper_path=providers_dir / f"{provider_name}.yaml",
        setup_script_path=provider_dir / "scripts" / "k8s" / "setup.sh",
        teardown_script_path=provider_dir / "scripts" / "k8s" / "teardown.sh",
        scope_template_path=provider_dir / "isv-readiness.k8s.scope.json",
        template_setup_script=template_dir / "setup.sh",
        template_teardown_script=template_dir / "teardown.sh",
    )


def write_k8s_onboarding_files(
    plan: K8sOnboardingPlan,
    *,
    overwrite: bool = False,
    preserve_existing_scripts: bool = False,
) -> list[Path]:
    written: list[Path] = []
    plan.provider_dir.joinpath("scripts", "k8s").mkdir(parents=True, exist_ok=True)

    _write_text(plan.wrapper_path, _wrapper_text(plan.provider_name), overwrite=overwrite)
    written.append(plan.wrapper_path)

    if not preserve_existing_scripts or not plan.setup_script_path.exists():
        _copy_or_write_script(
            source=plan.template_setup_script,
            target=plan.setup_script_path,
            fallback=_fallback_setup_script(),
            overwrite=overwrite,
        )
        written.append(plan.setup_script_path)

    if not preserve_existing_scripts or not plan.teardown_script_path.exists():
        _copy_or_write_script(
            source=plan.template_teardown_script,
            target=plan.teardown_script_path,
            fallback=_fallback_teardown_script(),
            overwrite=overwrite,
        )
        written.append(plan.teardown_script_path)

    _write_text(plan.scope_template_path, _scope_template_text(plan.provider_name), overwrite=overwrite)
    written.append(plan.scope_template_path)
    return written


def _validate_provider_name(provider_name: str) -> str:
    if not PROVIDER_NAME_RE.fullmatch(provider_name):
        raise ValueError("Provider name must contain only lowercase letters, numbers, '_' and '-'.")
    return provider_name


def _write_text(path: Path, text: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_or_write_script(*, source: Path, target: Path, fallback: str, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(source, target)
    else:
        target.write_text(fallback, encoding="utf-8")
    target.chmod(target.stat().st_mode | 0o111)


def _wrapper_text(provider_name: str) -> str:
    return f"""import: ../suites/k8s.yaml

version: "1.0"

commands:
  kubernetes:
    phases: ["setup", "test", "teardown"]
    steps:
      - name: setup
        phase: setup
        command: "{provider_name}/scripts/k8s/setup.sh"
        timeout: 120
      - name: teardown
        phase: teardown
        command: "{provider_name}/scripts/k8s/teardown.sh"
        timeout: 30

tests:
  description: "{provider_name} Kubernetes validation"
  platform: kubernetes
"""


def _scope_template_text(provider_name: str) -> str:
    return json.dumps(
        {
            "provider": provider_name,
            "owns": SCOPE_TEMPLATE_OWNS,
            "expected_skips": [],
            "run_env": "fill-me",
            "api_spec": None,
            "notes": "Fill ownership before allowing the agent to decide whether to patch, skip, or escalate K8s gaps.",
            "needed_isv_info": K8S_ISV_INFO_FIELDS,
        },
        indent=2,
    ) + "\n"


def _fallback_setup_script() -> str:
    return """#!/bin/sh
set -eu
printf '%s\n' '{"success": false, "platform": "kubernetes", "error": "TODO: implement K8s setup inventory"}'
exit 1
"""


def _fallback_teardown_script() -> str:
    return """#!/bin/sh
set -eu
printf '%s\n' '{"success": true, "platform": "kubernetes"}'
"""
