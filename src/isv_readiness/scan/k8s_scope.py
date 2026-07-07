from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from isv_readiness.scan.models import Status

K8S_LAYERS = (
    "cluster_lifecycle",
    "node_inventory",
    "node_pools",
    "gpu_operator",
    "network_policy",
    "storage_csi",
    "identity_oidc",
    "observability",
    "workloads",
    "api_network_acl",
)

VALIDATION_LAYER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("K8sNodePool", "node_pools"),
    ("K8sGpu", "gpu_operator"),
    ("K8sNvidia", "gpu_operator"),
    ("K8sDriver", "gpu_operator"),
    ("K8sMig", "gpu_operator"),
    ("K8sNetworkPolicy", "network_policy"),
    ("K8sCsi", "storage_csi"),
    ("K8sOidc", "identity_oidc"),
    ("K8sApiServerMetrics", "observability"),
    ("K8sControlPlaneLogs", "observability"),
    ("K8sNccl", "workloads"),
    ("K8sGpuStress", "workloads"),
    ("K8sNim", "workloads"),
    ("K8sApiNetworkAcl", "api_network_acl"),
    ("K8sNode", "node_inventory"),
    ("K8sExpectedNodes", "node_inventory"),
    ("K8sPodHealth", "node_inventory"),
    ("K8sNoPendingPods", "node_inventory"),
    ("K8sNoErrorPods", "node_inventory"),
    ("K8sDualStackNode", "node_inventory"),
    ("K8sClusterAutoscaler", "cluster_lifecycle"),
    ("K8sCncfConformance", "cluster_lifecycle"),
    ("K8sMultiCluster", "cluster_lifecycle"),
)

LAB_ENV_MARKERS = (
    "ngc_api_key",
    "mpi operator",
    "no gpu nodes",
    "no gpu nodes available",
    "no storage_class configured",
    "no csidriver objects",
    "not configured",
    "not found",
    "missing",
)

GPU_CAPABILITY_MARKERS = (
    "0 gpus",
    "no 'nvidia.com/gpu' resources",
    "no gpu nodes found",
    "driver version unknown",
)


@dataclass(frozen=True)
class K8sScope:
    provider: str | None = None
    owns: dict[str, bool] = field(default_factory=dict)
    expected_skips: list[str] = field(default_factory=list)
    api_spec: str | None = None
    run_env: str | None = None
    notes: str | None = None

    def owns_layer(self, layer: str | None) -> bool | None:
        if layer is None:
            return None
        return self.owns.get(layer)

    def to_enrichment(self, layer: str | None, classification_note: str) -> dict[str, Any]:
        return {
            "k8s_layer": layer,
            "k8s_layer_owned": self.owns_layer(layer),
            "classification_note": classification_note,
        }


@dataclass(frozen=True)
class K8sClassification:
    auto_fixable: bool
    layer: str | None
    note: str


def load_k8s_scope(path: Path | None) -> K8sScope:
    if path is None:
        return K8sScope()
    data = json.loads(path.read_text(encoding="utf-8"))
    owns = data.get("owns") or {}
    if not isinstance(owns, dict):
        owns = {}
    expected_skips = data.get("expected_skips") or []
    if not isinstance(expected_skips, list):
        expected_skips = []
    invalid_ownership = sorted(key for key, value in owns.items() if not isinstance(value, bool))
    if invalid_ownership:
        names = ", ".join(str(key) for key in invalid_ownership)
        raise ValueError(f"Kubernetes ownership values must be true or false; leave unknown keys absent: {names}")
    unknown_layers = sorted(set(str(key) for key in owns).difference(K8S_LAYERS))
    if unknown_layers:
        raise ValueError(f"Unknown Kubernetes ownership layers: {', '.join(unknown_layers)}")
    return K8sScope(
        provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
        owns={str(key): value for key, value in owns.items()},
        expected_skips=[str(item) for item in expected_skips],
        api_spec=data.get("api_spec") if isinstance(data.get("api_spec"), str) else None,
        run_env=data.get("run_env") if isinstance(data.get("run_env"), str) else None,
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
    )


def layer_for_validation(validation_class: str | None) -> str | None:
    if not validation_class:
        return None
    for prefix, layer in VALIDATION_LAYER_PREFIXES:
        if validation_class.startswith(prefix):
            return layer
    return None


def classify_k8s_gap(validation_class: str | None, status: Status, message: str, scope: K8sScope) -> K8sClassification:
    layer = layer_for_validation(validation_class)
    owned = scope.owns_layer(layer)
    lowered = message.lower()

    if status == "pass":
        return K8sClassification(False, layer, "Validation passed; no remediation route needed.")

    if validation_class in scope.expected_skips:
        return K8sClassification(False, layer, "Validation is listed as an expected skip by the ISV scope profile.")

    if status == "skipped":
        if owned is False:
            return K8sClassification(False, layer, "Skipped validation belongs to a layer the ISV marked out of scope.")
        if any(marker in lowered for marker in LAB_ENV_MARKERS):
            return K8sClassification(False, layer, "Skipped validation depends on missing lab/runtime configuration.")
        return K8sClassification(False, layer, "Skipped validation needs operator review before it can be fixed.")

    if any(marker in lowered for marker in GPU_CAPABILITY_MARKERS):
        if owned is False:
            return K8sClassification(False, layer, "GPU capability validation is outside the declared ISV scope.")
        if owned is True:
            return K8sClassification(False, layer, "GPU capability is owned by the ISV but not exposed to Kubernetes.")
        return K8sClassification(False, layer, "GPU capability is absent or not exposed in the current run environment.")

    if layer == "network_policy":
        if owned is False:
            return K8sClassification(False, layer, "NetworkPolicy is outside the declared ISV scope.")
        return K8sClassification(False, layer, "NetworkPolicy validation failed; this is a platform capability gap if in scope.")

    if "step_not_configured" in lowered or "not configured" in lowered:
        if owned is False:
            return K8sClassification(False, layer, "Validation step is not configured because the layer is out of scope.")
        return K8sClassification(True, layer, "Validation step is missing from the provider wrapper.")

    if owned is False:
        return K8sClassification(False, layer, "Gap belongs to a layer the ISV marked out of scope.")
    if owned is True:
        return K8sClassification(False, layer, "Gap belongs to an ISV-owned K8s layer and needs implementation or platform remediation.")
    return K8sClassification(False, layer, "Ownership is unknown; collect ISV scope before attempting a fix.")
