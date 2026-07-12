"""Tests for ITS manifest registry (no cluster)."""

from __future__ import annotations

from pathlib import Path

import pytest

from suite.errors import AppError
from suite.its_registry import (
    integration_test_scenario_application,
    integration_test_scenario_default_konflux_app,
    list_integration_test_scenario_manifests,
    resolve_integration_test_scenario_manifest,
    resolve_integration_test_scenario_run_now_snapshot,
    validate_integration_test_scenario_name,
)

_ROOT = Path(__file__).resolve().parents[2]


def test_validate_integration_test_scenario_name_ok() -> None:
    assert validate_integration_test_scenario_name("odh-olminstall-testops-eaas") == (
        "odh-olminstall-testops-eaas"
    )


def test_validate_integration_test_scenario_name_rejects_empty() -> None:
    with pytest.raises(AppError, match="non-empty"):
        validate_integration_test_scenario_name("  ")


def test_resolve_eaas_manifest() -> None:
    path = resolve_integration_test_scenario_manifest(_ROOT, "odh-olminstall-testops-eaas")
    assert path.name == "its-olminstall-testops-eaas.yaml"
    assert integration_test_scenario_application(path) == "testops-playpen"


def test_resolve_rh_nightly_manifest() -> None:
    path = resolve_integration_test_scenario_manifest(
        _ROOT,
        "odh-olminstall-testops-rh-nightly",
    )
    assert path.name == "its-olminstall-testops-rh-nightly.yaml"
    assert integration_test_scenario_application(path) == "rhoai-fbc-fragment-ocp-420"


def test_rh_nightly_default_konflux_app() -> None:
    assert (
        integration_test_scenario_default_konflux_app("odh-olminstall-testops-rh-nightly")
        == "rhoai-fbc-fragment-ocp-420"
    )
    assert integration_test_scenario_default_konflux_app("odh-olminstall-testops-eaas") == ""


def test_resolve_run_now_snapshot_rh_nightly() -> None:
    path = resolve_integration_test_scenario_run_now_snapshot(
        _ROOT,
        "odh-olminstall-testops-rh-nightly",
    )
    assert path.name == "test-snapshot-rh-nightly.yaml"


def test_resolve_run_now_snapshot_unsupported() -> None:
    with pytest.raises(AppError, match="no --run-now snapshot mapping"):
        resolve_integration_test_scenario_run_now_snapshot(_ROOT, "odh-olminstall-testops-eaas")


def test_resolve_unknown_manifest() -> None:
    with pytest.raises(AppError, match="No in-tree ITS manifest"):
        resolve_integration_test_scenario_manifest(_ROOT, "does-not-exist")


def test_list_manifests_includes_playpen_its() -> None:
    names = list_integration_test_scenario_manifests(_ROOT)
    assert "odh-olminstall-testops-eaas" in names
    assert "odh-olminstall-testops-rh-nightly" in names


def test_eaas_pipelinerun_wrapper_prefix() -> None:
    path = _ROOT / "tekton" / "pipelines" / "olminstall-pipelinerun-eaas.yaml"
    text = path.read_text(encoding="utf-8")
    assert "generateName: olminstall-its-eaas-bvt-smoke-" in text
