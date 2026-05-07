#!/usr/bin/env python3
"""
Merge quay.io/rhoai credentials into the EaaS cluster's global pull secret,
pre-create an imagePullSecret in openshift-marketplace for OLM pods, and
register an additional-pull-secret in kube-system for HyperShift node sync.

Internal Tekton pipeline step — not meant to be called directly.
From a laptop, trigger tests via: python3 …/run_olm_pipeline.py

In Tekton the quay secret is volume-mounted at /var/secret/quay/.dockerconfigjson.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

QUAY_SECRET_PATH = Path("/var/secret/quay/.dockerconfigjson")


def run_oc(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["oc", *args], check=check, capture_output=True, text=True)


def extract_quay_auth(auths: dict[str, Any]) -> str | None:
    for key in ("quay.io", "quay.io/rhoai", "quay.io/rhoai/rhoai-fbc-fragment"):
        ent = auths.get(key) or {}
        auth = ent.get("auth")
        if auth:
            return str(auth)
    for k, v in auths.items():
        if k.startswith("quay.io/rhoai/") and isinstance(v, dict) and v.get("auth"):
            return str(v["auth"])
    return None


def merge_docker_auths(existing: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    e_auth = dict(existing.get("auths") or {})
    o_auth = dict(overlay.get("auths") or {})
    out = dict(existing)
    out["auths"] = {**e_auth, **o_auth}
    return out


def main() -> int:
    if not QUAY_SECRET_PATH.is_file():
        print(f"❌ Quay secret not mounted at {QUAY_SECRET_PATH}")
        return 1

    quay = json.loads(QUAY_SECRET_PATH.read_text(encoding="utf-8"))
    auths = quay.get("auths") or {}
    quay_auth = extract_quay_auth(auths)
    if not quay_auth:
        print(f"❌ No quay.io/rhoai auth token found in {QUAY_SECRET_PATH}")
        return 1

    quay = merge_docker_auths(quay, {"auths": {"quay.io": {"auth": quay_auth}}})

    print("Patching cluster global pull secret with quay.io/rhoai credentials...")
    raw = run_oc(["get", "secret/pull-secret", "-n", "openshift-config", "-o", "json"]).stdout
    pull_data = json.loads(raw)
    b64 = pull_data["data"][".dockerconfigjson"]
    existing = json.loads(base64.standard_b64decode(b64))
    merged = merge_docker_auths(existing, quay)
    merged_raw = json.dumps(merged, separators=(",", ":")).encode()
    patch_b64 = base64.standard_b64encode(merged_raw).decode("ascii")
    payload = json.dumps({"data": {".dockerconfigjson": patch_b64}})
    subprocess.run(
        ["oc", "patch", "secret/pull-secret", "-n", "openshift-config", "--type=merge", "-p", payload],
        check=True,
    )
    print("✓ Global pull secret patched")

    print("Creating additional-pull-secret in kube-system (triggers HyperShift HCCO node sync)...")
    rhoai_entries = {k: v for k, v in (quay.get("auths") or {}).items() if k.startswith("quay.io/rhoai")}
    rhoai_auths = dict(rhoai_entries)
    rhoai_auths.setdefault("quay.io/rhoai", {"auth": quay_auth})
    rhoai_creds = {"auths": rhoai_auths}
    creds_json = json.dumps(rhoai_creds, separators=(",", ":"))
    p = subprocess.run(
        [
            "oc",
            "create",
            "secret",
            "generic",
            "additional-pull-secret",
            "-n",
            "kube-system",
            "--from-literal=.dockerconfigjson=" + creds_json,
            "--type=kubernetes.io/dockerconfigjson",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["oc", "apply", "-f", "-"], input=p.stdout, check=True, text=True)
    print("✓ additional-pull-secret created in kube-system")

    print("Creating rhoai-quay-pull imagePullSecret in openshift-marketplace for OLM SA-level pulls...")
    quay_json = json.dumps(quay, separators=(",", ":"))
    q = subprocess.run(
        [
            "oc",
            "create",
            "secret",
            "generic",
            "rhoai-quay-pull",
            "-n",
            "openshift-marketplace",
            "--from-literal=.dockerconfigjson=" + quay_json,
            "--type=kubernetes.io/dockerconfigjson",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["oc", "apply", "-f", "-"], input=q.stdout, check=True, text=True)

    ls = subprocess.run(
        ["oc", "get", "sa", "-n", "openshift-marketplace", "--no-headers", "-o", "custom-columns=:metadata.name"],
        capture_output=True,
        text=True,
    )
    if ls.returncode == 0:
        for line in ls.stdout.splitlines():
            name = line.strip()
            if name:
                subprocess.run(
                    ["oc", "secrets", "link", name, "rhoai-quay-pull", "-n", "openshift-marketplace", "--for=pull"],
                    capture_output=True,
                )
    print("✓ rhoai-quay-pull linked to all SAs in openshift-marketplace")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode or 1) from exc
