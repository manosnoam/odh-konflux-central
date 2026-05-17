#!/usr/bin/env python3
"""Emit pipeline-level TEST_OUTPUT from TaskRun results (Tekton finally task).

Reads the current PipelineRun via the in-cluster API so pipeline results need not
reference $(tasks.install-operator.results.*) when that task was skipped.

Priority: bvt-health-checks-with-eaas.TEST_OUTPUT, then bvt-health-checks-no-eaas.TEST_OUTPUT,
then install-operator.INSTALL_STATUS, else a short status fallback.

Env:
    RESULT_PATH -- Tekton result file path to write (required)
Optional:
    PIPELINE_RUN_NAME -- default: /etc/tekton/pipelineRunName (Tekton-injected)
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _in_cluster_get(url: str, token: str, ca_path: Path) -> dict[str, object]:
    ctx = ssl.create_default_context(cafile=str(ca_path))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected JSON object from API")
    return data


def _pipeline_run_name() -> str:
    for key in ("PIPELINE_RUN_NAME", "PIPELINERUN", "PIPELINE_RUN"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    p = Path("/etc/tekton/pipelineRunName")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    raise SystemExit("PIPELINE_RUN_NAME missing (and no /etc/tekton/pipelineRunName)")


def _namespace() -> str:
    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    v = os.environ.get("NAMESPACE", "").strip()
    if v:
        return v
    raise SystemExit("cannot determine namespace (no serviceAccount namespace file)")


def _pick_output(pr: dict[str, object]) -> str:
    ns = _namespace()
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="utf-8")
    ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    if not host:
        return "ERROR: KUBERNETES_SERVICE_HOST not set"
    base = f"https://{host}:{port}"

    meta = pr.get("metadata")
    if not isinstance(meta, dict):
        return "ERROR: PipelineRun missing metadata"
    pr_name = str(meta.get("name") or "")

    sel = urllib.parse.quote(f"tekton.dev/pipelineRun={pr_name}")
    url = f"{base}/apis/tekton.dev/v1/namespaces/{ns}/taskruns?labelSelector={sel}"
    try:
        tr_list = _in_cluster_get(url, token, ca)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        return f"ERROR: list TaskRuns: {exc}"

    items = tr_list.get("items")
    if not isinstance(items, list):
        return "ERROR: could not list TaskRuns for this PipelineRun"

    def task_name(tr: dict[str, object]) -> str:
        md = tr.get("metadata", {})
        if not isinstance(md, dict):
            return ""
        labels = md.get("labels", {})
        if not isinstance(labels, dict):
            return ""
        return str(labels.get("tekton.dev/pipelineTask", "") or "")

    def result_map(tr: dict[str, object]) -> dict[str, str]:
        out: dict[str, str] = {}
        status = tr.get("status", {})
        if not isinstance(status, dict):
            return out
        results = status.get("results")
        if not isinstance(results, list):
            return out
        for r in results:
            if not isinstance(r, dict):
                continue
            name = r.get("name")
            val = r.get("value")
            if isinstance(name, str) and isinstance(val, str):
                out[name] = val
        return out

    by_task: dict[str, dict[str, str]] = {}
    for tr in items:
        if not isinstance(tr, dict):
            continue
        tn = task_name(tr)
        if tn:
            by_task[tn] = result_map(tr)

    for prefer in ("bvt-health-checks-with-eaas", "bvt-health-checks-no-eaas"):
        res = by_task.get(prefer, {})
        if "TEST_OUTPUT" in res and res["TEST_OUTPUT"].strip():
            return res["TEST_OUTPUT"].strip()

    inst = by_task.get("install-operator", {})
    if "INSTALL_STATUS" in inst and inst["INSTALL_STATUS"].strip():
        return inst["INSTALL_STATUS"].strip()

    conds = pr.get("status", {})
    if isinstance(conds, dict):
        c = conds.get("conditions")
        if isinstance(c, list) and c:
            first = c[0]
            if isinstance(first, dict):
                return (
                    f"PipelineRun {pr_name}: {first.get('type', 'condition')}="
                    f"{first.get('status', '')} ({first.get('reason', '')})"
                )
    return f"PipelineRun {pr_name}: no TEST_OUTPUT/INSTALL_STATUS found on TaskRuns"


def main() -> int:
    result_path = os.environ.get("RESULT_PATH", "").strip()
    if not result_path:
        print("RESULT_PATH is required", file=sys.stderr)
        return 1

    pr_name = _pipeline_run_name()
    ns = _namespace()
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="utf-8")
    ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip()
    if not host:
        print("KUBERNETES_SERVICE_HOST not set", file=sys.stderr)
        return 1
    base = f"https://{host}:{port}"
    pr_url = f"{base}/apis/tekton.dev/v1/namespaces/{ns}/pipelineruns/{urllib.parse.quote(pr_name)}"
    try:
        pr = _in_cluster_get(pr_url, token, ca)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        print(f"ERROR: get PipelineRun: {exc}", file=sys.stderr)
        return 1

    text = _pick_output(pr)
    Path(result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(result_path).write_text(text, encoding="utf-8")
    print(f"Wrote pipeline TEST_OUTPUT ({len(text)} chars) to {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
