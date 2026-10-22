"""MLflow tracking URI overrides when rh-ai route is unreachable from Tekton."""

from __future__ import annotations

import json
import os
import shlex

from suite.its_trigger_params import CLUSTER_SOURCE_EPHC, is_external_cluster_source

# test-run.sh honors FORCE_PORT_FORWARD=true on OpenShift (kubectl port-forward + PF_PID).
# Gateway mode sets ARTIFACTS_SERVER_GATEWAY during the run; decide at shell runtime, not only at orchestrate.
_SHELL_PREFIX = (
    'if [ "${ARTIFACTS_SERVER_GATEWAY:-}" != "true" ]'
    ' && [ "${ARTIFACTS_SERVER_GATEWAY:-}" != "1" ]; then export FORCE_PORT_FORWARD=true; fi'
)


def _mlflow_tracking_patch_enabled() -> bool:
    source = os.environ.get("CLUSTER_SOURCE", "").strip()
    return source == CLUSTER_SOURCE_EPHC or is_external_cluster_source(source)


def mlflow_crd_supports_artifacts_server() -> bool:
    """True when the installed MLflow CRD accepts spec.artifactsServer (gateway artifacts path)."""
    try:
        from install.dsc_install import oc_run
    except Exception:
        return False
    r = oc_run(
        ["get", "crd", "mlflows.mlflow.opendatahub.io", "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return False
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    for ver in data.get("spec", {}).get("versions", []) or []:
        if not isinstance(ver, dict):
            continue
        schema = ver.get("schema", {}).get("openAPIV3Schema", {})
        if not isinstance(schema, dict):
            continue
        spec_props = schema.get("properties", {}).get("spec", {}).get("properties", {})
        if isinstance(spec_props, dict) and "artifactsServer" in spec_props:
            return True
    return False


def _artifacts_server_gateway_enabled() -> bool:
    if os.environ.get("ARTIFACTS_SERVER_GATEWAY", "").strip().lower() in (
        "true",
        "1",
        "yes",
    ):
        return True
    # test-run.sh sets ARTIFACTS_SERVER_GATEWAY when the cluster uses the gateway
    # artifacts path; probe here so we do not prepend FORCE_PORT_FORWARD in bash -c.
    try:
        from install.dsc_install import uses_aigateway_models_as_a_service

        if not uses_aigateway_models_as_a_service():
            return False
        return mlflow_crd_supports_artifacts_server()
    except Exception:
        return False


def mlflow_orchestrate_env_defaults(
    env_defaults: dict[str, str] | None,
) -> dict[str, str] | None:
    """Pin gateway off when the operator CRD cannot accept spec.artifactsServer."""
    if _artifacts_server_gateway_enabled():
        return env_defaults
    out = dict(env_defaults or {})
    out["ARTIFACTS_SERVER_GATEWAY"] = "false"
    return out


def _use_force_port_forward() -> bool:
    if _artifacts_server_gateway_enabled():
        return False
    return _mlflow_tracking_patch_enabled()


def mlflow_ephc_incluster_tracking_shell() -> str:
    """Tell test-run.sh to port-forward instead of using the MLflow CR route."""
    if not _use_force_port_forward():
        return ""
    return _SHELL_PREFIX


def prepend_mlflow_ephc_tracking(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    if not _use_force_port_forward():
        return cmd
    return f"{_SHELL_PREFIX} && {cmd}"


def prepend_mlflow_run_wall_clock(run_command: str, timeout_sec: float) -> str:
    """Kill hung pytest/port-forward retries (Tekton task timeout is much higher than smoke budget)."""
    cmd = (run_command or "").strip()
    if not cmd or timeout_sec <= 0:
        return cmd
    secs = max(60, int(timeout_sec))
    return f"timeout --foreground -s TERM {secs}s bash -c {shlex.quote(cmd)}"
