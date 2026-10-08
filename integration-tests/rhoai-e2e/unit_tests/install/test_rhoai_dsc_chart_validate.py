"""Tests for operator-pinned chart DSC validation."""

from __future__ import annotations

import unittest
from unittest import mock

from install.rhoai_dsc_chart_validate import (
    fetch_manifests_config,
    infer_operator_git_ref,
    install_removed_keys_from_promotion_policy,
    parse_build_config_pin,
    parse_component_names_policy,
    resolve_pinned_chart_context,
    validate_dsc_keys_supported_by_chart,
    validate_smoke_managed_keys_for_operator_version,
)
from suite.errors import AppError


MANIFESTS_CONFIG = """
buildConfig:
  rhoai:
    ref: rhoai-3.5@abc123def
components:
  ogx:
    rhoai:
      repo: red-hat-data-services/ogx-k8s-operator
"""

CHART_VALUES = """
components:
  dashboard:
    dsc:
      managementState: Managed
  kserve:
    dsc:
      managementState: Managed
  trainer:
    dsc:
      managementState: Managed
  sparkoperator:
    dsc:
      managementState: Removed
"""


class RhoaiDscChartValidateTest(unittest.TestCase):
    def test_infer_operator_git_ref(self) -> None:
        self.assertEqual(infer_operator_git_ref("3.5.1"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref("v3.5.2"), "rhoai-3.5")
        self.assertEqual(infer_operator_git_ref(""), "main")

    def test_parse_build_config_pin(self) -> None:
        display, fetch = parse_build_config_pin(MANIFESTS_CONFIG)
        self.assertEqual(display, "rhoai-3.5@abc123def")
        self.assertEqual(fetch, "abc123def")

    def test_fetch_manifests_config_fallback_path(self) -> None:
        def fetch(url: str) -> str:
            if "/manifests-config.yaml" in url and "/build/" not in url:
                raise AppError("HTTP GET failed: 404. Not Found", 2)
            return MANIFESTS_CONFIG

        yaml_text, url = fetch_manifests_config("rhoai-3.5", fetch)
        self.assertIn("/build/manifests-config.yaml", url)
        self.assertIn("buildConfig", yaml_text)

    def test_validate_chart_supports_keys(self) -> None:
        import yaml

        doc = yaml.safe_load(CHART_VALUES)
        validate_dsc_keys_supported_by_chart({"dashboard", "kserve", "trainer"}, doc)
        with self.assertRaises(AppError):
            validate_dsc_keys_supported_by_chart({"ogx"}, doc)

    def test_resolve_pinned_chart_context_with_mock_fetch(self) -> None:
        def fetch(url: str) -> str:
            if "manifests-config" in url or "build/manifests-config" in url:
                return MANIFESTS_CONFIG
            return CHART_VALUES

        ctx = resolve_pinned_chart_context("3.5.1", fetch_text_fn=fetch)
        self.assertEqual(ctx.operator_git_ref, "rhoai-3.5")
        self.assertEqual(ctx.build_config_fetch_ref, "abc123def")
        self.assertIn("/abc123def/", ctx.values_yaml_url)

    def test_validate_smoke_managed_keys_reports(self) -> None:
        def fetch(url: str) -> str:
            if "manifests-config" in url or "build/manifests-config" in url:
                return MANIFESTS_CONFIG
            return CHART_VALUES

        report = validate_smoke_managed_keys_for_operator_version(
            {"dashboard", "trainer"},
            "3.5.1",
            fetch_text_fn=fetch,
        )
        self.assertTrue(any(line.startswith("valuesYamlUrl=") for line in report))

    def test_parse_component_names_policy(self) -> None:
        policy = parse_component_names_policy("trainer:Managed,sparkoperator:Removed")
        self.assertEqual(policy["trainer"], "Managed")
        self.assertEqual(policy["sparkoperator"], "Removed")

    def test_install_removed_from_promotion_policy(self) -> None:
        removed = install_removed_keys_from_promotion_policy(
            "trainer:Managed,sparkoperator:Removed,trainingoperator:Removed"
        )
        self.assertIn("sparkoperator", removed)
        self.assertIn("trainingoperator", removed)
        self.assertNotIn("trainer", removed)

    @mock.patch("install.dsc_install_policy._maybe_validate_managed_keys_against_chart")
    def test_resolve_managed_dsc_keys_skips_validate_without_version(self, _validate: object) -> None:
        from install.dsc_install_policy import resolve_managed_dsc_keys

        keys = resolve_managed_dsc_keys("trainer", "", for_install=False)
        self.assertIn("trainer", keys)


if __name__ == "__main__":
    unittest.main()
