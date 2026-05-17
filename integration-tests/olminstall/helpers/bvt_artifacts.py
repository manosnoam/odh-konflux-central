"""Resolve BVT artifact browser URLs from PipelineRun / TaskRun state."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def tests_include_bvt(tests_csv: str) -> bool:
    return "bvt" in {p.strip().lower() for p in (tests_csv or "").split(",") if p.strip()}


def _pipeline_run_name_from_env() -> str:
    for key in ("PIPELINE_RUN_NAME", "PIPELINERUN", "PIPELINE_RUN"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    p = Path("/etc/tekton/pipelineRunName")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _namespace_from_env() -> str:
    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return os.environ.get("NAMESPACE", "").strip()


def _in_cluster_get(url: str, token: str, ca_path: Path) -> dict[str, Any]:
    ctx = ssl.create_default_context(cafile=str(ca_path))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object from API")
    return data


def _task_name(tr: dict[str, Any]) -> str:
    labels = (tr.get("metadata") or {}).get("labels") or {}
    if not isinstance(labels, dict):
        return ""
    return str(labels.get("tekton.dev/pipelineTask", "") or "")


def _task_reason(tr: dict[str, Any]) -> str:
    for cond in (tr.get("status") or {}).get("conditions") or []:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "Succeeded":
            return str(cond.get("reason") or "").strip()
    return ""


def _result_map(tr: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in (tr.get("status") or {}).get("results") or []:
        if not isinstance(r, dict):
            continue
        name, val = r.get("name"), r.get("value")
        if isinstance(name, str) and isinstance(val, str):
            out[name] = val
    return out


def list_taskruns_in_cluster(pipeline_run: str, namespace: str) -> list[dict[str, Any]]:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    if not (pipeline_run and namespace and token_path.is_file() and ca_path.is_file() and host):
        return []
    token = token_path.read_text(encoding="utf-8")
    sel = urllib.parse.quote(f"tekton.dev/pipelineRun={pipeline_run}")
    url = (
        f"https://{host}:{port}/apis/tekton.dev/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/taskruns?labelSelector={sel}"
    )
    try:
        doc = _in_cluster_get(url, token, ca_path)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return []
    items = doc.get("items")
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


def published_artifacts_url_from_taskruns(taskruns: list[dict[str, Any]]) -> str:
    for tr in taskruns:
        task = _task_name(tr).lower()
        if "bvt-health-checks" not in task:
            continue
        url = _result_map(tr).get("ARTIFACTS_URL", "").strip()
        if url:
            return url
    return ""


def bvt_unpublished_reason(taskruns: list[dict[str, Any]]) -> str:
    """Short explanation when BVT was requested but no ARTIFACTS_URL was published."""
    bvt_tasks = [_task_name(tr) for tr in taskruns if "bvt-health-checks" in _task_name(tr).lower()]
    if not bvt_tasks:
        return "BVT did not run (pipeline failed or was skipped before bvt-health-checks)"
    for tr in taskruns:
        task = _task_name(tr)
        if "bvt-health-checks" not in task.lower():
            continue
        reason = _task_reason(tr)
        if reason == "Completed":
            if "upload-artifacts" in task.lower() or task.endswith("upload-artifacts"):
                return "upload-artifacts finished but did not publish ARTIFACTS_URL"
            return "BVT finished without publishing artifacts (upload-artifacts may have been skipped)"
        if reason in ("Failed", "PipelineRunFailed", "TaskRunFailed"):
            return f"{task} failed — see TaskRun logs"
        if reason in ("Cancelled", "TaskRunCancelled", "PipelineRunCancelled"):
            return f"{task} was cancelled"
        if reason in ("Skipped", "TaskRunSkipped", "PipelineRunSkipped"):
            return f"{task} was skipped"
    return "BVT did not publish JUnit to the artifact browser"


def resolve_artifacts_notification_line(
    *,
    tests_csv: str,
    pipeline_run: str,
    taskruns: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return a single notification line, or None to omit artifacts entirely."""
    if not tests_include_bvt(tests_csv):
        return None
    runs = taskruns if taskruns is not None else []
    if not runs and pipeline_run:
        ns = _namespace_from_env()
        if ns:
            runs = list_taskruns_in_cluster(pipeline_run, ns)
    published = published_artifacts_url_from_taskruns(runs)
    if published:
        return f"Artifacts: {published}"
    return f"Artifacts: (none — {bvt_unpublished_reason(runs)})"
