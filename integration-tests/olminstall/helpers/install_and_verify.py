#!/usr/bin/env python3
"""
Install the RHOAI operator via OLM from a Konflux FBCF image and verify the CSV
reaches Succeeded status.

Internal Tekton pipeline step — not meant to be called directly.
From a laptop, trigger tests via: python3 …/run_olm_pipeline.py

OLM install manifests come from ${OLMINSTALL_DIR} (cloned olminstall repo).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

FBCF_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9./_:@-]+$")
NS_PATCH_PATTERN = re.compile(r"^(\s*namespace:\s*)redhat-ods-operator\s*$", re.MULTILINE)
OC_WAIT_NEEDLE = 'local namespace="${2:-default}"'


def fail(message: str = "") -> NoReturn:
    if message:
        print(message)
    p = os.environ.get("INSTALL_STATUS_PATH")
    if p:
        try:
            Path(p).write_text("FAILED", encoding="utf-8")
        except OSError:
            pass
    sys.exit(1)


def require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        fail(f"❌ Required environment variable is missing: {name}")
    return v


def run_oc(args: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["oc", *args], check=check, capture_output=capture, text=True)


def validate_fbcf_image(ref: str) -> None:
    if not FBCF_IMAGE_PATTERN.fullmatch(ref):
        fail(f"❌ FBCF_IMAGE contains unexpected characters: {ref}")


def patch_oc_wait_sh(olminstall_dir: Path, operator_namespace: str) -> None:
    path = olminstall_dir / "utils" / "oc_wait.sh"
    text = path.read_text(encoding="utf-8")
    replacement = f'local namespace="${{2:-{operator_namespace}}}"'
    if OC_WAIT_NEEDLE not in text:
        fail(f"❌ Expected snippet not found in {path}")
    path.write_text(text.replace(OC_WAIT_NEEDLE, replacement, 1), encoding="utf-8")


def patch_manifest_namespace(manifest_path: Path, operator_namespace: str) -> None:
    content = manifest_path.read_text(encoding="utf-8")
    patched = NS_PATCH_PATTERN.sub(rf"\g<1>{operator_namespace}", content)
    manifest_path.write_text(patched, encoding="utf-8")


def resolve_olminstall_manifest(olminstall_dir: Path, operator_name: str) -> tuple[Path, str]:
    manifest = olminstall_dir / "resources" / f"install-{operator_name}.yaml"
    if manifest.is_file():
        return manifest, operator_name
    fallback = olminstall_dir / "resources" / "install-rhods-operator.yaml"
    if fallback.is_file():
        print(f"⚠ Manifest install-{operator_name}.yaml not found — using rhods-operator manifest")
        return fallback, "rhods-operator"
    fail(f"❌ No olminstall manifest found for operator {operator_name}")


def apply_catalog_source(name: str, fbcf_image: str) -> None:
    yaml_doc = f"""apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: {name}
  namespace: openshift-marketplace
spec:
  sourceType: grpc
  image: {fbcf_image}
  displayName: RHOAI Dev Catalog
  publisher: Red Hat
  updateStrategy:
    registryPoll:
      interval: 30m
  grpcPodConfig:
    securityContextConfig: legacy
"""
    subprocess.run(["oc", "apply", "-f", "-"], input=yaml_doc, check=True, text=True)


def wait_for_sa(sa_name: str, namespace: str, deadline_s: float) -> bool:
    while time.time() < deadline_s:
        r = subprocess.run(["oc", "get", "sa", sa_name, "-n", namespace], capture_output=True)
        if r.returncode == 0:
            return True
        time.sleep(5)
    return False


def catalog_connection_state(catalog_name: str) -> str:
    r = subprocess.run(
        [
            "oc",
            "get",
            "catalogsource",
            catalog_name,
            "-n",
            "openshift-marketplace",
            "-o",
            "jsonpath={.status.connectionState.lastObservedState}",
        ],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def copy_pull_secret(secret_name: str, dest_namespace: str) -> bool:
    r = subprocess.run(
        ["oc", "get", "secret", secret_name, "-n", "openshift-marketplace", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    obj: dict[str, Any] = json.loads(r.stdout)
    md = dict(obj.get("metadata") or {})
    for k in ("uid", "resourceVersion", "creationTimestamp", "managedFields", "ownerReferences", "generation"):
        md.pop(k, None)
    md.pop("selfLink", None)
    ann = dict(md.get("annotations") or {})
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if ann:
        md["annotations"] = ann
    else:
        md.pop("annotations", None)
    md["namespace"] = dest_namespace
    obj["metadata"] = md
    p = subprocess.run(["oc", "apply", "-f", "-"], input=json.dumps(obj), capture_output=True, text=True)
    return p.returncode == 0


def link_secret_to_all_sas(secret_name: str, namespace: str) -> None:
    r = subprocess.run(
        ["oc", "get", "sa", "-n", namespace, "--no-headers", "-o", "custom-columns=:metadata.name"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return
    for line in r.stdout.splitlines():
        name = line.strip()
        if name:
            subprocess.run(
                ["oc", "secrets", "link", name, secret_name, "-n", namespace, "--for=pull"],
                capture_output=True,
            )


def wait_global_pull_secret_syncer() -> None:
    print("Waiting for HyperShift HCCO to sync quay.io/rhoai credentials to all nodes (up to 5m)...")
    sync_desired = 0
    for i in range(1, 25):
        chk = subprocess.run(
            ["oc", "get", "daemonset", "global-pull-secret-syncer", "-n", "kube-system"],
            capture_output=True,
        )
        if chk.returncode == 0:
            r = subprocess.run(
                [
                    "oc",
                    "get",
                    "ds",
                    "global-pull-secret-syncer",
                    "-n",
                    "kube-system",
                    "-o",
                    "jsonpath={.status.desiredNumberScheduled}",
                ],
                capture_output=True,
                text=True,
            )
            try:
                sync_desired = int((r.stdout or "0").strip() or "0")
            except ValueError:
                sync_desired = 0
            if sync_desired > 0:
                break
        print(f"  waiting for global-pull-secret-syncer DaemonSet... (check {i}/24)")
        time.sleep(5)

    if sync_desired == 0:
        print("⚠ global-pull-secret-syncer DaemonSet not found after 2m — HCCO feature may not be")
        print("  available on this cluster version. Proceeding; bundle-unpack may fail with ErrImagePull.")
        return

    print(f"  global-pull-secret-syncer desired={sync_desired}")
    sync_ready = 0
    ready_deadline = time.time() + 180
    while time.time() < ready_deadline:
        r = subprocess.run(
            [
                "oc",
                "get",
                "ds",
                "global-pull-secret-syncer",
                "-n",
                "kube-system",
                "-o",
                "jsonpath={.status.numberReady}",
            ],
            capture_output=True,
            text=True,
        )
        try:
            sync_ready = int((r.stdout or "0").strip() or "0")
        except ValueError:
            sync_ready = 0
        print(f"  nodes synced: {sync_ready}/{sync_desired}")
        if sync_ready >= max(sync_desired, 1):
            print(f"✓ quay.io/rhoai credentials synced to all {sync_desired} nodes")
            return
        time.sleep(10)

    print(f"⚠ Syncer incomplete after 3m ({sync_ready}/{sync_desired} nodes) — proceeding")
    subprocess.run(
        ["oc", "get", "pods", "-n", "kube-system", "-l", "name=global-pull-secret-syncer", "--no-headers"],
        capture_output=True,
        text=True,
    )


def pick_succeeded_csv_version(namespace: str, olminstall_operator: str) -> str | None:
    r = subprocess.run(["oc", "get", "csv", "-n", namespace, "-o", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    op_pat = re.compile(re.escape(olminstall_operator), re.I)
    for item in data.get("items") or []:
        if (item.get("status") or {}).get("phase") != "Succeeded":
            continue
        md_name = (item.get("metadata") or {}).get("name") or ""
        disp = ((item.get("spec") or {}).get("displayName")) or ""
        if md_name.startswith(olminstall_operator) or (disp and op_pat.search(disp)):
            ver = (item.get("spec") or {}).get("version")
            if ver:
                return str(ver)
    return None


def wait_catalog_ready(catalog_name: str, deadline_s: float) -> bool:
    cs_status = ""
    iteration = 0
    while time.time() < deadline_s:
        cs_status = catalog_connection_state(catalog_name)
        if cs_status == "READY":
            print("✓ CatalogSource READY")
            return True
        iteration += 1
        print(f"  CS state: {cs_status or 'unknown'} (iter {iteration})")
        if iteration % 4 == 0:
            pr = subprocess.run(
                [
                    "oc",
                    "get",
                    "pods",
                    "-n",
                    "openshift-marketplace",
                    "-l",
                    f"olm.catalogSource={catalog_name}",
                    "--no-headers",
                    "-o",
                    "custom-columns=:metadata.name",
                ],
                capture_output=True,
                text=True,
            )
            pod = (pr.stdout.splitlines()[0].strip() if pr.stdout else "") or ""
            if pod:
                subprocess.run(["oc", "get", "pod", pod, "-n", "openshift-marketplace"])
                ev = subprocess.run(
                    [
                        "oc",
                        "get",
                        "events",
                        "-n",
                        "openshift-marketplace",
                        "--field-selector",
                        f"involvedObject.name={pod}",
                    ],
                    capture_output=True,
                    text=True,
                )
                lines = ev.stdout.splitlines()
                for line in lines[-3:]:
                    print(line)
            else:
                print("  no CatalogSource pod yet")
                subprocess.run(["oc", "get", "pods", "-n", "openshift-marketplace", "--no-headers"])
        time.sleep(15)
    return False


def main() -> int:
    install_status_path = require_env("INSTALL_STATUS_PATH")
    operator_namespace = require_env("OPERATOR_NAMESPACE")
    operator_name = require_env("OPERATOR_NAME")
    update_channel = require_env("UPDATE_CHANNEL")
    fbcf_image = require_env("FBCF_IMAGE")
    olminstall_dir_s = require_env("OLMINSTALL_DIR")
    operator_version_path = require_env("OPERATOR_VERSION_PATH")

    catalog_name = os.environ.get("OLMINSTALL_CATALOG_NAME", "rhoai-catalog-dev")
    quay_pull_secret = os.environ.get("QUAY_PULL_SECRET_NAME", "rhoai-quay-pull")

    olminstall_dir = Path(olminstall_dir_s)

    print("=========================================")
    print(" ODH/RHOAI Operator Installation")
    print(f" FBCF:      {fbcf_image}")
    print(f" Channel:   {update_channel}")
    print(f" Operator:  {operator_name} -> {operator_namespace}")
    print("=========================================")

    try:
        subprocess.run(["oc", "version"], check=True, capture_output=False)
    except subprocess.CalledProcessError:
        fail("❌ Cannot connect to cluster")

    ns_yaml = subprocess.run(
        ["oc", "create", "namespace", operator_namespace, "--dry-run=client", "-o", "yaml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(["oc", "apply", "-f", "-"], input=ns_yaml, check=True, text=True)

    validate_fbcf_image(fbcf_image)

    print("Creating CatalogSource (legacy security context)...")
    apply_catalog_source(catalog_name, fbcf_image)

    print(f"Waiting for OLM to create the {catalog_name} ServiceAccount (up to 2m)...")
    if not wait_for_sa(catalog_name, "openshift-marketplace", time.time() + 120):
        print(f"⚠ ServiceAccount {catalog_name} not observed within 2m")

    lk = subprocess.run(
        ["oc", "secrets", "link", catalog_name, quay_pull_secret, "-n", "openshift-marketplace", "--for=pull"],
        capture_output=True,
    )
    if lk.returncode != 0:
        print(
            f"⚠ Could not link {quay_pull_secret} to {catalog_name} SA "
            "(SA may not exist yet — non-fatal)"
        )

    print(f"Restarting CatalogSource pod to pick up the {quay_pull_secret} SA secret...")
    subprocess.run(
        [
            "oc",
            "delete",
            "pod",
            "-n",
            "openshift-marketplace",
            "-l",
            f"olm.catalogSource={catalog_name}",
            "--ignore-not-found=true",
        ],
        capture_output=True,
    )
    subprocess.run(
        ["oc", "wait", "--for=delete", "pod", "-n", "openshift-marketplace", "-l", f"olm.catalogSource={catalog_name}", "--timeout=60s"],
        capture_output=True,
    )

    print("Waiting for CatalogSource to be READY (up to 15m)...")
    if not wait_catalog_ready(catalog_name, time.time() + 900):
        print("❌ CatalogSource not READY after timeout")
        subprocess.run(["oc", "describe", "catalogsource", catalog_name, "-n", "openshift-marketplace"])
        pr = subprocess.run(
            [
                "oc",
                "get",
                "pods",
                "-n",
                "openshift-marketplace",
                "-l",
                f"olm.catalogSource={catalog_name}",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
        )
        cs_pod = (pr.stdout or "").strip()
        if cs_pod:
            subprocess.run(["oc", "describe", "pod", cs_pod, "-n", "openshift-marketplace"])
        fail()

    print(f"Copying {quay_pull_secret} to {operator_namespace} and linking to all SAs...")
    if not copy_pull_secret(quay_pull_secret, operator_namespace):
        print(f"⚠ Failed to copy {quay_pull_secret} to {operator_namespace} — OLM SA-level pulls may fail")
    link_secret_to_all_sas(quay_pull_secret, operator_namespace)

    wait_global_pull_secret_syncer()

    patch_oc_wait_sh(olminstall_dir, operator_namespace)
    manifest_path, olminstall_operator = resolve_olminstall_manifest(olminstall_dir, operator_name)
    patch_manifest_namespace(manifest_path, operator_namespace)

    print(
        f"Running olminstall (./install-operator.sh {olminstall_operator} {update_channel} {catalog_name})..."
    )
    r_install = subprocess.run(
        ["./install-operator.sh", olminstall_operator, update_channel, catalog_name],
        cwd=olminstall_dir,
    )
    if r_install.returncode != 0:
        print("❌ olminstall install-operator.sh failed")
        subprocess.run(["oc", "get", "sub,csv,installplan", "-n", operator_namespace])
        subprocess.run(["oc", "describe", "sub", "-n", operator_namespace])
        fail()

    csv_version = pick_succeeded_csv_version(operator_namespace, olminstall_operator)
    if not csv_version or csv_version == "unknown":
        print(f"❌ No CSV reached Succeeded phase in namespace {operator_namespace}")
        subprocess.run(["oc", "get", "csv", "-n", operator_namespace])
        fail()
    Path(operator_version_path).write_text(csv_version, encoding="utf-8")

    print("")
    print("=========================================")
    print(" Installation Results")
    print("=========================================")
    print(f" Operator version : {csv_version}")
    print(f" Namespace        : {operator_namespace}")
    print(f" Channel          : {update_channel}")
    print(f" FBCF image       : {fbcf_image}")
    print("-----------------------------------------")
    print(" CSV status:")
    subprocess.run(
        [
            "oc",
            "get",
            "csv",
            "-n",
            operator_namespace,
            "-o",
            "custom-columns=NAME:.metadata.name,PHASE:.status.phase,VERSION:.spec.version",
        ]
    )
    print("-----------------------------------------")
    print(" Operator deployment:")
    subprocess.run(
        [
            "oc",
            "get",
            "deployment",
            "-n",
            operator_namespace,
            "-o",
            "custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,"
            "AVAILABLE:.status.availableReplicas,IMAGE:.spec.template.spec.containers[0].image",
        ]
    )
    print("-----------------------------------------")
    print(" Installed CRDs (rhoai):")
    cr = subprocess.run(["oc", "get", "crd", "-o", "json"], capture_output=True, text=True)
    if cr.returncode == 0:
        data = json.loads(cr.stdout)
        pat = re.compile(r"opendatahub|datasciencecluster|rhoai|kfdef", re.I)
        for item in data.get("items") or []:
            name = (item.get("metadata") or {}).get("name") or ""
            if pat.search(name):
                print(f"  {name}")
    print("=========================================")
    print(f"✅ Installation complete — operator version: {csv_version}")
    Path(install_status_path).write_text("SUCCESS", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        fail("❌ Interrupted")
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, file=sys.stderr, end="" if exc.stderr.endswith("\n") else "\n")
        if exc.stdout:
            print(exc.stdout, file=sys.stderr, end="" if exc.stdout.endswith("\n") else "\n")
        fail(f"❌ Command failed (exit {exc.returncode})")
