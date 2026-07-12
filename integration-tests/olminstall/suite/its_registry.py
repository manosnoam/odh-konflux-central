"""Resolve in-tree IntegrationTestScenario manifests by metadata.name."""

from __future__ import annotations

import re
from pathlib import Path

from suite.errors import AppError

_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# metadata.name -> config snapshot YAML for ``--enable-its NAME --run-now``
_ITS_RUN_NOW_SNAPSHOT_BY_NAME: dict[str, str] = {
    "odh-olminstall-testops-rh-nightly": "config/test-snapshot-rh-nightly.yaml",
}

# metadata.name -> Konflux Application when ``--konflux-app`` differs from DEFAULT_APP
_ITS_DEFAULT_KONFLUX_APP_BY_NAME: dict[str, str] = {
    "odh-olminstall-testops-rh-nightly": "rhoai-fbc-fragment-ocp-420",
}


def validate_integration_test_scenario_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise AppError("IntegrationTestScenario name must be non-empty.", 2)
    if not _K8S_NAME_RE.fullmatch(text):
        raise AppError(
            f"Invalid IntegrationTestScenario name {text!r}; use a valid Kubernetes resource name.",
            2,
        )
    return text


def _its_dir(olminstall_root: Path) -> Path:
    return olminstall_root / "tekton" / "its"


def _load_manifest_doc(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise AppError("PyYAML is required to read ITS manifests.", 1) from exc
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AppError(f"Cannot read ITS manifest {path}: {exc}", 1) from exc
    except yaml.YAMLError as exc:
        raise AppError(f"Invalid YAML in ITS manifest {path}: {exc}", 1) from exc
    if not isinstance(doc, dict):
        raise AppError(f"ITS manifest {path} is empty or not a mapping.", 1)
    return doc


def integration_test_scenario_default_konflux_app(name: str) -> str:
    """Return the Konflux Application for ``--enable-its NAME`` when not testops-playpen."""
    validated = validate_integration_test_scenario_name(name)
    return _ITS_DEFAULT_KONFLUX_APP_BY_NAME.get(validated, "")


def integration_test_scenario_application(manifest_path: Path) -> str:
    doc = _load_manifest_doc(manifest_path)
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("application", "")).strip()


def list_integration_test_scenario_manifests(olminstall_root: Path) -> dict[str, Path]:
    """Map metadata.name -> manifest path for all YAML files under tekton/its/."""
    out: dict[str, Path] = {}
    its_dir = _its_dir(olminstall_root)
    if not its_dir.is_dir():
        return out
    for path in sorted(its_dir.glob("*.yaml")):
        doc = _load_manifest_doc(path)
        if doc.get("kind") != "IntegrationTestScenario":
            continue
        meta = doc.get("metadata")
        if not isinstance(meta, dict):
            continue
        name = str(meta.get("name", "")).strip()
        if name:
            out[name] = path
    return out


def resolve_integration_test_scenario_manifest(olminstall_root: Path, name: str) -> Path:
    """Return manifest path for a known ITS name; raise AppError when missing."""
    validated = validate_integration_test_scenario_name(name)
    its_dir = _its_dir(olminstall_root)
    indexed = list_integration_test_scenario_manifests(olminstall_root)
    path = indexed.get(validated)
    if path is not None:
        return path
    known = ", ".join(sorted(indexed)) or "(none)"
    raise AppError(
        f"No in-tree ITS manifest for {validated!r} under {its_dir}. Known names: {known}",
        2,
    )


def format_known_integration_test_scenario_names(olminstall_root: Path) -> str:
    return ", ".join(sorted(list_integration_test_scenario_manifests(olminstall_root)))


def resolve_integration_test_scenario_run_now_snapshot(olminstall_root: Path, name: str) -> Path:
    """Return Snapshot manifest for ``--enable-its NAME --run-now`` offline fallback."""
    validated = validate_integration_test_scenario_name(name)
    rel = _ITS_RUN_NOW_SNAPSHOT_BY_NAME.get(validated)
    if not rel:
        supported = ", ".join(sorted(_ITS_RUN_NOW_SNAPSHOT_BY_NAME)) or "(none)"
        raise AppError(
            f"IntegrationTestScenario {validated!r} has no --run-now snapshot mapping. "
            f"Supported: {supported}",
            2,
        )
    path = olminstall_root / rel
    if not path.is_file():
        raise AppError(f"--run-now snapshot file missing: {path}", 1)
    return path
