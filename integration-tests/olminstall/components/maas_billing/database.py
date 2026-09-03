"""MaaS database secret setup (models-as-a-service parity)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from install.dsc_install import oc_run
from install.dependency_operators import unblock_terminating_namespace
from steps.tekton_util import git_clone

from components.maas_billing.common import (
    _MAAS_APPS_NS,
    _MAAS_API_NS_CANDIDATES,
    _MAAS_DB_SECRET,
    _MODELS_AS_SERVICE_DEST,
    _MODELS_AS_SERVICE_REPO,
    _kubectl_shim_dir,
    _secret_exists,
    maas_api_namespace,
)

_MAAS_INFRA_NS = "odh-ai-gateway-infra"
_MAAS_TENANT_NS = "models-as-a-service"
_DEFAULT_MAAS_INFRA_CLEANUP_TIMEOUT_SEC = 300


def _maas_infra_namespace() -> str:
    return os.environ.get("MAAS_INFRA_NAMESPACE", _MAAS_INFRA_NS).strip() or _MAAS_INFRA_NS


def _read_secret_data_key(namespace: str, secret_name: str, key: str) -> str | None:
    r = oc_run(
        ["get", "secret", secret_name, "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    try:
        body = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    encoded = (body.get("data") or {}).get(key)
    if not encoded:
        return None
    return base64.b64decode(encoded).decode("utf-8", errors="replace").strip() or None


def _maas_postgres_service() -> str:
    return os.environ.get("MAAS_POSTGRES_SERVICE", "postgres").strip() or "postgres"


def _postgres_host_for_apps_namespace(infra_ns: str | None = None) -> str:
    ns = (infra_ns or _maas_infra_namespace()).strip()
    service = _maas_postgres_service()
    return f"{service}.{ns}.svc.cluster.local"


def _rewrite_db_connection_url_for_apps_namespace(
    connection_url: str,
    *,
    infra_ns: str | None = None,
) -> str:
    """maas-api runs in apps ns; Postgres from setup-database.sh is in the infra ns."""
    infra = (infra_ns or _maas_infra_namespace()).strip()
    service = _maas_postgres_service()
    target_host = _postgres_host_for_apps_namespace(infra)
    parsed = urlparse(connection_url)
    if not parsed.hostname:
        return connection_url
    if parsed.hostname == target_host:
        return connection_url
    ns_svc_host = f"{service}.{infra}.svc"
    if parsed.hostname not in (service, ns_svc_host):
        return connection_url
    port = parsed.port or 5432
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"
    netloc = f"{auth}{target_host}:{port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def _repair_apps_maas_db_connection_url_if_needed() -> bool:
    """Update apps maas-db-config when it still points at infra-local postgres hostname."""
    current = _read_secret_data_key(_MAAS_APPS_NS, _MAAS_DB_SECRET, "DB_CONNECTION_URL")
    if not current:
        return False
    repaired = _rewrite_db_connection_url_for_apps_namespace(current)
    if repaired == current:
        return False
    print(
        f"Repairing {_MAAS_APPS_NS}/{_MAAS_DB_SECRET} DB host for cross-namespace maas-api "
        f"({urlparse(current).hostname} -> {urlparse(repaired).hostname})",
        flush=True,
    )
    _create_maas_db_config_secret(_MAAS_APPS_NS, repaired)
    return True


def _restart_maas_api_after_db_config() -> None:
    """Roll maas-api so it picks up maas-db-config in redhat-ods-applications."""
    ns = maas_api_namespace()
    r = oc_run(
        ["get", "deployment", "maas-api", "-n", ns],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return
    from components.maas_billing.auth import (
        _rollout_restart_deployment,
        _wait_maas_api_deployment_ready,
    )

    rollout_timeout = int(os.environ.get("MAAS_API_ROLLOUT_TIMEOUT_SEC", "300"))
    ready_timeout = int(os.environ.get("MAAS_API_READY_TIMEOUT_SEC", "600"))
    print(
        f"Rolling out maas-api in {ns} after {_MAAS_DB_SECRET} update...",
        flush=True,
    )
    _rollout_restart_deployment(
        ns,
        "maas-api",
        timeout_sec=rollout_timeout,
    )
    _wait_maas_api_deployment_ready(timeout_sec=ready_timeout)


def _promote_maas_db_secret_to_apps_namespace() -> bool:
    """Copy maas-db-config into redhat-ods-applications when setup-database.sh left it in infra."""
    if _secret_exists(_MAAS_APPS_NS, _MAAS_DB_SECRET):
        return True
    infra_ns = _maas_infra_namespace()
    if not _namespace_exists(infra_ns) or not _secret_exists(infra_ns, _MAAS_DB_SECRET):
        return False
    connection_url = _read_secret_data_key(infra_ns, _MAAS_DB_SECRET, "DB_CONNECTION_URL")
    if not connection_url:
        return False
    connection_url = _rewrite_db_connection_url_for_apps_namespace(
        connection_url,
        infra_ns=infra_ns,
    )
    print(
        f"Promoting {_MAAS_DB_SECRET} from {infra_ns} to {_MAAS_APPS_NS} "
        "(setup-database.sh deploys Postgres in the infra namespace)",
        flush=True,
    )
    _create_maas_db_config_secret(_MAAS_APPS_NS, connection_url)
    return _secret_exists(_MAAS_APPS_NS, _MAAS_DB_SECRET)


def _create_maas_db_config_secret(namespace: str, connection_url: str) -> None:
    created = oc_run(
        [
            "create",
            "secret",
            "generic",
            _MAAS_DB_SECRET,
            "--from-file=DB_CONNECTION_URL=/dev/stdin",
            "-n",
            namespace,
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        stdin_text=connection_url,
        check=True,
        capture_output=True,
        timeout=60,
    )
    apply = oc_run(
        ["apply", "-f", "-"],
        stdin_text=created.stdout or "",
        check=False,
        capture_output=True,
        timeout=60,
    )
    if apply.returncode != 0:
        err = (apply.stderr or apply.stdout or "").strip()
        raise RuntimeError(f"Could not apply {_MAAS_DB_SECRET}: {err or 'unknown error'}")
    oc_run(
        ["label", "secret", _MAAS_DB_SECRET, "-n", namespace, "app=maas-api", "--overwrite"],
        check=False,
        capture_output=True,
        timeout=30,
    )


def _clone_models_as_a_service() -> Path:
    dest = _MODELS_AS_SERVICE_DEST
    if dest.exists():
        shutil.rmtree(dest)
    rev = os.environ.get("MODELS_AS_SERVICE_REPO_REVISION", "").strip() or "main"
    print(f"Cloning models-as-a-service for MaaS DB setup ({_MODELS_AS_SERVICE_REPO} @ {rev})...", flush=True)
    git_clone(_MODELS_AS_SERVICE_REPO, rev, dest, tls_workaround=True)
    return dest


def _namespace_exists(name: str) -> bool:
    r = oc_run(
        ["get", "namespace", name],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def _namespace_phase(name: str) -> str | None:
    r = oc_run(
        ["get", "namespace", name, "-o", "jsonpath={.status.phase}"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return None
    phase = (r.stdout or "").strip()
    return phase or None


def _wait_namespace_deleted(name: str, *, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        phase = _namespace_phase(name)
        if phase is None:
            return
        if phase == "Terminating":
            unblock_terminating_namespace(name)
        time.sleep(5)
    phase = _namespace_phase(name)
    if phase is not None:
        raise RuntimeError(
            f"namespace {name} still {phase} after {timeout_sec}s (MaaS infra cleanup)"
        )


def _delete_maas_db_secrets() -> None:
    infra_ns = _maas_infra_namespace()
    for ns in (_MAAS_APPS_NS, infra_ns):
        if not _secret_exists(ns, _MAAS_DB_SECRET):
            continue
        oc_run(
            ["delete", "secret", _MAAS_DB_SECRET, "-n", ns, "--ignore-not-found"],
            check=False,
            capture_output=True,
            timeout=60,
        )
        print(f"✓ Removed stale MaaS DB secret {ns}/{_MAAS_DB_SECRET}", flush=True)


def _delete_namespace_if_present(name: str, *, wait: bool, timeout_sec: int) -> None:
    if not _namespace_exists(name):
        return
    print(f"Deleting namespace {name} (stale MaaS infra)...", flush=True)
    oc_run(
        ["delete", "namespace", name, "--wait=false"],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if wait:
        _wait_namespace_deleted(name, timeout_sec=timeout_sec)


def cleanup_maas_postgres_infra(*, wait: bool = True) -> None:
    """Remove MaaS Postgres/DB secrets and infra namespace before operator cleanup."""
    timeout_sec = int(
        os.environ.get("MAAS_INFRA_CLEANUP_TIMEOUT_SEC", str(_DEFAULT_MAAS_INFRA_CLEANUP_TIMEOUT_SEC))
    )
    _delete_maas_db_secrets()
    _delete_namespace_if_present(_maas_infra_namespace(), wait=wait, timeout_sec=timeout_sec)


def cleanup_maas_tenant_namespace(*, wait: bool = True) -> None:
    """Remove MaaS tenant namespace after operator cleanup.

    Operators can recreate ``models-as-a-service`` while RHCL/MaaS CSVs still exist;
    run this only after ``cleanup.sh -t operator``.
    """
    timeout_sec = int(
        os.environ.get("MAAS_INFRA_CLEANUP_TIMEOUT_SEC", str(_DEFAULT_MAAS_INFRA_CLEANUP_TIMEOUT_SEC))
    )
    _delete_namespace_if_present(_MAAS_TENANT_NS, wait=wait, timeout_sec=timeout_sec)


def cleanup_maas_database_infra(*, wait: bool = True) -> None:
    """Remove pooled-cluster MaaS Postgres/DB secrets left after operator cleanup.

    olminstall ``cleanup.sh -t operator`` does not delete ``odh-ai-gateway-infra``;
    reusing its Postgres leaves ``schema_migrations`` from a prior MaaS version and
    breaks ``maas-api`` on the next RHOAI install.
    """
    cleanup_maas_postgres_infra(wait=wait)
    cleanup_maas_tenant_namespace(wait=wait)


def _postgres_deploy_ready(infra_ns: str) -> bool:
    r = oc_run(
        [
            "get",
            "deployment",
            _maas_postgres_service(),
            "-n",
            infra_ns,
            "-o",
            "jsonpath={.status.readyReplicas}",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if r.returncode != 0:
        return False
    try:
        return int((r.stdout or "0").strip() or "0") >= 1
    except ValueError:
        return False


def _read_maas_postgres_schema_version() -> int | None:
    infra_ns = _maas_infra_namespace()
    if not _namespace_exists(infra_ns) or not _postgres_deploy_ready(infra_ns):
        return None
    proc = oc_run(
        [
            "exec",
            "-n",
            infra_ns,
            f"deploy/{_maas_postgres_service()}",
            "--",
            "psql",
            "-U",
            "maas",
            "-d",
            "maas",
            "-tAc",
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw or raw.lower() == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _maas_api_deployment_ready() -> bool:
    for ns in _MAAS_API_NS_CANDIDATES:
        r = oc_run(
            [
                "get",
                "deployment",
                "maas-api",
                "-n",
                ns,
                "-o",
                "jsonpath={.status.readyReplicas}",
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0:
            continue
        try:
            if int((r.stdout or "0").strip() or "0") >= 1:
                return True
        except ValueError:
            continue
    return False


def _maas_postgres_has_missing_schema() -> bool:
    """True when infra Postgres is up but schema_migrations was never created."""
    infra_ns = _maas_infra_namespace()
    if not _postgres_deploy_ready(infra_ns):
        return False
    proc = oc_run(
        [
            "exec",
            "-n",
            infra_ns,
            f"deploy/{_maas_postgres_service()}",
            "--",
            "psql",
            "-U",
            "maas",
            "-d",
            "maas",
            "-tAc",
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        ],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode == 0:
        return False
    combined = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
    return "schema_migrations" in combined and "does not exist" in combined


def _needs_maas_postgres_reset() -> bool:
    if _maas_api_deployment_ready():
        return False
    version = _read_maas_postgres_schema_version()
    if version is not None and version > 0:
        print(
            f"WARN: MaaS Postgres schema_migrations version={version} with maas-api not ready "
            "(likely incompatible with current maas-api image)",
            flush=True,
        )
        return True
    if _maas_postgres_has_missing_schema():
        print(
            "WARN: MaaS Postgres is running without schema_migrations while maas-api is not ready "
            "(resetting stale infra before setup-database.sh)",
            flush=True,
        )
        return True
    return False


def _apps_namespace_ready_for_secrets() -> bool:
    """True when redhat-ods-applications exists and accepts creates (not Terminating).

    After cleanup-external the apps NS often sticks Terminating; promoting maas-db-config
    then fails with Forbidden. Wait it out (unblock finalizers) and treat as missing.
    """
    phase = _namespace_phase(_MAAS_APPS_NS)
    if phase is None:
        return False
    if phase == "Active":
        return True
    if phase == "Terminating":
        timeout = int(os.environ.get("MAAS_APPS_NS_DELETE_TIMEOUT_SEC", "300"))
        print(
            f"NOTE: {_MAAS_APPS_NS} is Terminating; waiting up to {timeout}s before MaaS DB setup...",
            flush=True,
        )
        _wait_namespace_deleted(_MAAS_APPS_NS, timeout_sec=timeout)
        return False
    print(
        f"WARN: {_MAAS_APPS_NS} phase={phase}; treating as not ready for {_MAAS_DB_SECRET}",
        flush=True,
    )
    return False


def ensure_maas_database() -> None:
    """Ensure maas-db-config exists in redhat-ods-applications (models-as-a-service parity)."""
    if not _apps_namespace_ready_for_secrets():
        from install.dependency_operators import product_install_path

        if product_install_path():
            # Jenkins creates redhat-ods-applications + postgres before setup.sh
            print(
                f"Creating {_MAAS_APPS_NS} before RHOAI install (Jenkins MaaS prep parity)...",
                flush=True,
            )
            create = oc_run(
                [
                    "create",
                    "namespace",
                    _MAAS_APPS_NS,
                    "--dry-run=client",
                    "-o",
                    "yaml",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if create.returncode == 0 and (create.stdout or "").strip():
                apply = oc_run(
                    ["apply", "-f", "-"],
                    stdin_text=create.stdout,
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
                if apply.returncode != 0:
                    err = (apply.stderr or apply.stdout or "").strip()
                    print(
                        f"WARN: could not create {_MAAS_APPS_NS}: {err[:200]}; "
                        "deferring maas-db-config until post install-rhoai",
                        flush=True,
                    )
                    return
            if not _apps_namespace_ready_for_secrets():
                print(
                    f"NOTE: deferring {_MAAS_DB_SECRET} until {_MAAS_APPS_NS} is Active "
                    "(post install-rhoai; prepare-components will retry)",
                    flush=True,
                )
                return
        else:
            raise RuntimeError(
                f"namespace {_MAAS_APPS_NS} not found; cannot ensure {_MAAS_DB_SECRET}"
            )

    if _secret_exists(_MAAS_APPS_NS, _MAAS_DB_SECRET):
        if _needs_maas_postgres_reset():
            cleanup_maas_database_infra()
        else:
            if _repair_apps_maas_db_connection_url_if_needed():
                print(
                    f"✓ MaaS database secret {_MAAS_APPS_NS}/{_MAAS_DB_SECRET} repaired for apps-namespace maas-api",
                    flush=True,
                )
                _restart_maas_api_after_db_config()
            else:
                print(f"✓ MaaS database secret {_MAAS_APPS_NS}/{_MAAS_DB_SECRET} exists", flush=True)
            return

    external_url = os.environ.get("MAAS_DB_CONNECTION_URL", "").strip()
    if external_url:
        print(f"Creating {_MAAS_DB_SECRET} from MAAS_DB_CONNECTION_URL...", flush=True)
        _create_maas_db_config_secret(_MAAS_APPS_NS, external_url)
        print(f"✓ MaaS database secret {_MAAS_APPS_NS}/{_MAAS_DB_SECRET} created", flush=True)
        _restart_maas_api_after_db_config()
        return

    repo = _clone_models_as_a_service()
    script = repo / "scripts" / "setup-database.sh"
    if not script.is_file():
        raise FileNotFoundError(f"Missing MaaS setup script: {script}")

    env = os.environ.copy()
    env["MAAS_CONTROLLER_NAMESPACE"] = _MAAS_APPS_NS
    env["DB_SSLMODE"] = "disable"
    env["PATH"] = f"{_kubectl_shim_dir()}:{env.get('PATH', '')}"
    print(
        f"Running setup-database.sh (MAAS_CONTROLLER_NAMESPACE={_MAAS_APPS_NS}, DB_SSLMODE=disable)...",
        flush=True,
    )
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            cwd=repo,
            env=env,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("setup-database.sh timed out after 600s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"setup-database.sh failed (exit {proc.returncode})")
    if not _secret_exists(_MAAS_APPS_NS, _MAAS_DB_SECRET):
        if not _promote_maas_db_secret_to_apps_namespace():
            raise RuntimeError(
                f"{_MAAS_DB_SECRET} still missing in {_MAAS_APPS_NS} after setup-database.sh"
            )
    print(f"✓ MaaS database ready ({_MAAS_APPS_NS}/{_MAAS_DB_SECRET})", flush=True)
    _restart_maas_api_after_db_config()
