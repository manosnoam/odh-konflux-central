"""ROSA HCP install-data fallback: rosa-admin password from openshift-cli-installer S3 zip."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from install.kubeconfig_cluster_label import cluster_name_from_url
from k8s.external_credentials import ExternalClusterCredentials
from k8s.jenkins_vault import VAULT_AUTH_MOUNT, load_hcp_install_aws_credentials
from steps.tekton_util import _kubeconfig_api_server

DEFAULT_S3_BUCKET = "hcp-clusters-mdata"
DEFAULT_S3_PREFIX = "openshift-cli-installer/"
ROSA_ADMIN_USER = "rosa-admin"
ZIP_PASSWORD_ENTRY = "auth/rosa-admin-password"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def resolve_install_cluster_name(bootstrap_path: Path) -> str:
    """Map bootstrap kubeconfig API URL to openshift-cli-installer zip basename."""
    override = _env("ROSA_HCP_INSTALL_CLUSTER_NAME")
    if override:
        return override
    if not bootstrap_path.is_file():
        return ""
    server = _kubeconfig_api_server(bootstrap_path)
    if not server:
        return ""
    return cluster_name_from_url(server)


def _s3_object_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket.rstrip('/')}/{key.lstrip('/')}"


def _install_zip_s3_key(cluster_name: str) -> str:
    prefix = _env("ROSA_HCP_INSTALL_S3_PREFIX", DEFAULT_S3_PREFIX)
    return f"{prefix.rstrip('/')}/{cluster_name}.zip"


def _extract_zip_member(zip_bytes: bytes, member_path: str) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        try:
            raw = archive.read(member_path)
        except KeyError:
            return ""
    return raw.decode("utf-8").strip()


def _s3_download_bytes(bucket: str, key: str, aws_env: dict[str, str]) -> bytes:
    access_key = aws_env.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = aws_env.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not access_key or not secret_key:
        return b""
    region = (
        aws_env.get("AWS_DEFAULT_REGION", "").strip()
        or _env("AWS_DEFAULT_REGION", "us-east-1")
    )
    try:
        import boto3

        client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        response = client.get_object(Bucket=bucket, Key=key)
        body = response.get("Body")
        return body.read() if body is not None else b""
    except Exception as exc:
        print(f"WARN: boto3 S3 download failed for {_s3_object_uri(bucket, key)}: {exc}", flush=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        dest = Path(tmp.name)
    env = {**os.environ, **aws_env}
    proc = subprocess.run(
        ["aws", "s3", "cp", _s3_object_uri(bucket, key), str(dest)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(
            f"WARN: aws s3 cp failed for {_s3_object_uri(bucket, key)}: {detail or proc.returncode}",
            flush=True,
        )
        dest.unlink(missing_ok=True)
        return b""
    try:
        return dest.read_bytes()
    finally:
        dest.unlink(missing_ok=True)


def load_rosa_admin_credentials_from_install_zip(
    *,
    bootstrap_path: Path,
    auth_dir: Path = VAULT_AUTH_MOUNT,
) -> ExternalClusterCredentials | None:
    """Return rosa-admin credentials from openshift-cli-installer S3 zip when available."""
    cluster_name = resolve_install_cluster_name(bootstrap_path)
    api_server = _kubeconfig_api_server(bootstrap_path) if bootstrap_path.is_file() else ""
    if not cluster_name or not api_server:
        return None

    aws_env = load_hcp_install_aws_credentials(auth_dir=auth_dir)
    if not aws_env.get("AWS_ACCESS_KEY_ID") or not aws_env.get("AWS_SECRET_ACCESS_KEY"):
        print("WARN: AWS credentials unavailable for ROSA HCP install-data S3 fallback", flush=True)
        return None

    bucket = _env("ROSA_HCP_INSTALL_S3_BUCKET", DEFAULT_S3_BUCKET)
    key = _install_zip_s3_key(cluster_name)
    zip_bytes = _s3_download_bytes(bucket, key, aws_env)
    if not zip_bytes:
        return None

    password = _extract_zip_member(zip_bytes, ZIP_PASSWORD_ENTRY)
    if not password:
        print(
            f"WARN: {ZIP_PASSWORD_ENTRY!r} missing in {_s3_object_uri(bucket, key)}",
            flush=True,
        )
        return None

    print(
        f"Loaded rosa-admin password from {_s3_object_uri(bucket, key)} "
        f"(cluster={cluster_name})",
        flush=True,
    )
    return ExternalClusterCredentials(
        username=ROSA_ADMIN_USER,
        password=password,
        api_server=api_server,
    )
