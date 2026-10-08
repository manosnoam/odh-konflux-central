"""Validate smoke DSC keys against operator-pinned RHOAI-Build-Config chart (Jenkins RhoaiDscComponentsResolver parity)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from suite.errors import AppError

_FETCH_USER_AGENT = "rhoai-e2e-dsc-chart-validate"
_DEFAULT_CONNECT_TIMEOUT_SEC = 30
_DEFAULT_READ_TIMEOUT_SEC = 120

RHOAI_OPERATOR_GITHUB = "red-hat-data-services/rhods-operator"
RHOAI_BUILD_CONFIG_GITHUB = "red-hat-data-services/RHOAI-Build-Config"
MANIFESTS_CONFIG_PATHS = ("manifests-config.yaml", "build/manifests-config.yaml")
CHART_VALUES_PATH = "to-be-processed/helm/rhai-on-openshift-chart/values.yaml"

# Jenkins GateJobParams.GATE_DSC_CR_COMPONENT_ALIASES — chart inventory may use CR names.
CHART_COMPONENT_KEY_ALIASES: dict[str, str] = {
    "aipipelines": "datasciencepipelines",
}

# Policy summary keys that live under components.<parent>.dsc.<nested> in chart values.
NESTED_CHART_DSC_PATHS: dict[str, tuple[str, str]] = {
    "modelsasservice": ("aigateway", "modelsAsAService"),
    "batchgateway": ("aigateway", "batchGateway"),
}


@dataclass(frozen=True)
class PinnedChartContext:
    operator_git_ref: str
    build_config_display_ref: str
    build_config_fetch_ref: str
    values_yaml_url: str
    values_doc: dict[str, Any]


def chart_validation_enabled() -> bool:
    raw = os.environ.get("DSC_CHART_VALIDATE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def infer_operator_git_ref(operator_version: str) -> str:
    fallback = os.environ.get("RHOAI_OPERATOR_GIT_REF", "").strip() or "main"
    ver = (operator_version or "").strip()
    if not ver:
        return fallback
    normalized = re.sub(r"^[vV]", "", ver)
    match = re.match(r"^(\d+)\.(\d+)", normalized)
    if match:
        return f"rhoai-{match.group(1)}.{match.group(2)}"
    return fallback


def raw_github_content_url(owner_repo: str, git_ref: str, repo_relative_path: str) -> str:
    path = repo_relative_path.strip().lstrip("/")
    ref = git_ref.strip()
    return f"https://raw.githubusercontent.com/{owner_repo}/{ref}/{path}"


def _urlopen_timeout_sec(connect_timeout_sec: int, read_timeout_sec: int) -> float:
    """urllib accepts (connect, read) tuples only on Python 3.11+ (Tekton install uses 3.9)."""
    if sys.version_info >= (3, 11):
        return (float(connect_timeout_sec), float(read_timeout_sec))  # type: ignore[return-value]
    return float(connect_timeout_sec) + float(read_timeout_sec)


def fetch_text(url: str, *, connect_timeout_sec: int = _DEFAULT_CONNECT_TIMEOUT_SEC) -> str:
    read_timeout = int(
        os.environ.get("DSC_CHART_VALIDATE_READ_TIMEOUT_SEC", str(_DEFAULT_READ_TIMEOUT_SEC))
    )
    req = urllib.request.Request(url, headers={"User-Agent": _FETCH_USER_AGENT})
    try:
        with urllib.request.urlopen(
            req, timeout=_urlopen_timeout_sec(connect_timeout_sec, read_timeout)
        ) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise AppError(f"HTTP GET {url} failed: {exc.code}. {body}", 2) from exc
    except urllib.error.URLError as exc:
        raise AppError(f"HTTP GET {url} failed: {exc.reason}", 2) from exc


def fetch_manifests_config(
    operator_git_ref: str,
    fetch_text_fn: Callable[[str], str] = fetch_text,
) -> tuple[str, str]:
    failures: list[str] = []
    for path in MANIFESTS_CONFIG_PATHS:
        url = raw_github_content_url(RHOAI_OPERATOR_GITHUB, operator_git_ref, path)
        try:
            yaml_text = fetch_text_fn(url).strip()
            if yaml_text:
                return yaml_text, url
        except AppError as exc:
            if "404" in str(exc):
                failures.append(url)
                continue
            raise
    raise AppError(
        f"Operator manifests config not found for ref {operator_git_ref} "
        f"(tried {', '.join(MANIFESTS_CONFIG_PATHS)}): {'; '.join(failures)}",
        2,
    )


def _ensure_chart_validate_yaml_loader() -> None:
    """install-rhoai image often has neither PyYAML nor yq; bootstrap like component pytest."""
    try:
        import yaml  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
        return
    except ImportError:
        pass
    if shutil.which("yq"):
        return
    from helpers.pip_bootstrap import pip_install_to_target, prepend_pythonpath
    from steps.tests_payload import resolve_tests_payload_root, tests_payload_tools_python_dir

    artifacts = os.environ.get("ARTIFACTS_DIR", "").strip() or "/workspace/tests-shared"
    target = tests_payload_tools_python_dir(resolve_tests_payload_root(artifacts))
    print(f"Installing PyYAML to {target} (DSC chart validation)...", flush=True)
    pip_install_to_target("pyyaml", target)
    prepend_pythonpath(str(target))
    import yaml  # type: ignore[import-untyped, unused-ignore]  # noqa: F401


def parse_build_config_pin(manifests_yaml: str) -> tuple[str, str]:
    doc = _load_yaml_document_from_text(manifests_yaml)
    ref = str((doc.get("buildConfig") or {}).get("rhoai", {}).get("ref") or "").strip()
    if not ref:
        return "", ""
    if "@" in ref:
        branch_part, commit_part = ref.split("@", 1)
        commit = commit_part.strip()
        return ref, commit or branch_part.strip()
    return ref, ref


def _load_yaml_document_from_text(yaml_text: str) -> dict[str, Any]:
    if not yaml_text.strip():
        return {}
    loaded = _load_yaml_document_from_string(yaml_text)
    return loaded if isinstance(loaded, dict) else {}


def _load_yaml_document_from_string(yaml_text: str) -> Any:
    text = yaml_text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(yaml_text)
        return loaded if loaded is not None else {}
    except ImportError:
        pass
    except Exception as exc:
        raise AppError(f"Invalid YAML document: {exc}", 2) from exc

    yq_bin = shutil.which("yq")
    if yq_bin:
        try:
            proc = subprocess.run(
                [yq_bin, "e", "-o=json", "."],
                input=yaml_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"yq timed out parsing YAML (>{exc.timeout}s)", 2) from exc
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise AppError(f"Invalid JSON from yq: {exc}", 2) from exc
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AppError(f"yq failed to parse YAML: {detail or proc.returncode}", 2)

    raise AppError(
        "DSC chart validation requires PyYAML or yq in the install-rhoai task image",
        2,
    )


def resolve_pinned_chart_context(
    operator_version: str,
    *,
    operator_git_ref: str = "",
    fetch_text_fn: Callable[[str], str] = fetch_text,
) -> PinnedChartContext:
    _ensure_chart_validate_yaml_loader()
    op_ref = (operator_git_ref or infer_operator_git_ref(operator_version)).strip()
    manifests_yaml, _manifests_url = fetch_manifests_config(op_ref, fetch_text_fn)
    display_ref, fetch_ref = parse_build_config_pin(manifests_yaml)
    if not fetch_ref:
        fetch_ref = os.environ.get("RHOAI_BUILD_CONFIG_GIT_REF", "").strip() or op_ref
    values_url = raw_github_content_url(RHOAI_BUILD_CONFIG_GITHUB, fetch_ref, CHART_VALUES_PATH)
    values_yaml = fetch_text_fn(values_url)
    values_doc = _load_yaml_document_from_text(values_yaml)
    if not values_doc:
        raise AppError(f"Pinned chart values empty or unreadable: {values_url}", 2)
    return PinnedChartContext(
        operator_git_ref=op_ref,
        build_config_display_ref=display_ref,
        build_config_fetch_ref=fetch_ref,
        values_yaml_url=values_url,
        values_doc=values_doc,
    )


def _chart_has_component(chart_components: dict[str, Any], dsc_key: str) -> bool:
    key = dsc_key.strip().lower()
    if key in NESTED_CHART_DSC_PATHS:
        parent, nested = NESTED_CHART_DSC_PATHS[key]
        dsc = (chart_components.get(parent) or {}).get("dsc") if isinstance(chart_components.get(parent), dict) else None
        return isinstance(dsc, dict) and nested in dsc
    alias = CHART_COMPONENT_KEY_ALIASES.get(key, key)
    return alias in chart_components


def validate_dsc_keys_supported_by_chart(
    dsc_keys: set[str],
    values_doc: dict[str, Any],
) -> None:
    chart_components = values_doc.get("components")
    if not isinstance(chart_components, dict):
        raise AppError("Pinned chart values.yaml has no components mapping", 2)
    errors: list[str] = []
    for key in sorted(dsc_keys):
        if not _chart_has_component(chart_components, key):
            alias = CHART_COMPONENT_KEY_ALIASES.get(key, key)
            if key in NESTED_CHART_DSC_PATHS:
                parent, nested = NESTED_CHART_DSC_PATHS[key]
                errors.append(
                    f"smoke DSC key '{key}' requires components.{parent}.dsc.{nested} in pinned values.yaml"
                )
            else:
                errors.append(
                    f"smoke DSC key '{key}' requires components.{alias} in pinned values.yaml"
                )
    if errors:
        raise AppError(
            f"Pinned chart validation failed ({len(errors)}): {'; '.join(errors)}",
            2,
        )


@lru_cache(maxsize=8)
def _cached_pinned_chart(operator_version: str, operator_git_ref: str) -> PinnedChartContext:
    return resolve_pinned_chart_context(operator_version, operator_git_ref=operator_git_ref)


def validate_smoke_managed_keys_for_operator_version(
    managed_keys: set[str],
    operator_version: str,
    *,
    operator_git_ref: str = "",
    fetch_text_fn: Callable[[str], str] | None = None,
) -> list[str]:
    """Return configuration report lines; raise AppError when validation fails."""
    if not managed_keys or not (operator_version or "").strip():
        return []
    if fetch_text_fn is not None:
        ctx = resolve_pinned_chart_context(
            operator_version,
            operator_git_ref=operator_git_ref,
            fetch_text_fn=fetch_text_fn,
        )
    else:
        ctx = _cached_pinned_chart(operator_version.strip(), (operator_git_ref or "").strip())
    validate_dsc_keys_supported_by_chart(managed_keys, ctx.values_doc)
    return [
        f"operatorGitRef={ctx.operator_git_ref}",
        f"buildConfigRef={ctx.build_config_display_ref or '(none)'}",
        f"buildConfigFetchRef={ctx.build_config_fetch_ref}",
        f"valuesYamlUrl={ctx.values_yaml_url}",
        f"validatedSmokeDscKeys={len(managed_keys)}",
    ]


def parse_component_names_policy(policy_string: str) -> dict[str, str]:
    """Parse Jenkins COMPONENT_NAMES (key:Managed|Unmanaged|Removed, comma-separated)."""
    out: dict[str, str] = {}
    for entry in (policy_string or "").split(","):
        part = entry.strip()
        if not part:
            continue
        key, _, mode = part.partition(":")
        name = key.strip().lower()
        if not name:
            continue
        out[name] = (mode.strip() or "Managed")
    return out


def install_removed_keys_from_promotion_policy(
    policy_string: str,
    *,
    dsc_cr_key: bool = True,
) -> frozenset[str]:
    """DSC spec keys that promotion-gate policy marks Removed (for install-time deferral)."""
    removed: set[str] = set()
    cr_to_dsc = {
        "aipipelines": "aipipelines",
        "datasciencepipelines": "aipipelines",
        "trainer": "trainer",
        "trainingoperator": "trainingoperator",
        "modelregistry": "modelregistry",
    }
    for key, state in parse_component_names_policy(policy_string).items():
        if state != "Removed":
            continue
        if key in NESTED_CHART_DSC_PATHS:
            continue
        spec_key = cr_to_dsc.get(key, key) if dsc_cr_key else key
        removed.add(spec_key)
    return frozenset(removed)
