"""Unit tests for Snapshot catalog-line detection (no cluster)."""

from __future__ import annotations

import unittest

from suite.snapshot_catalog_line import (
    catalog_line_from_prname,
    catalog_line_from_snapshot_metadata,
    catalog_line_meets_min_version,
)

_LABELS_225 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-225-ocp-421-on-push",
}
_ANNOTATIONS_225 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-2.25",
    "test.appstudio.openshift.io/result-image-url": (
        "quay.io/rhoai/rhoai-fbc-fragment:ocp-4.21-rhoai-2.25-b9f86dc5ee8d2c4e4a146593ac54336531889f9a"
    ),
    "pac.test.appstudio.openshift.io/on-cel-expression": (
        'event == "push" && "catalog/rhoai-2.25/v4.21/rhods-operator/catalog.yaml".pathChanged()'
    ),
}

_LABELS_35 = {
    "pac.test.appstudio.openshift.io/original-prname": "rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push",
}
_ANNOTATIONS_35 = {
    "pac.test.appstudio.openshift.io/sha-title": "Patching the stage catalog with rhoai-3.5-ea.2",
    "test.appstudio.openshift.io/result-image-url": (
        "quay.io/rhoai/rhoai-fbc-fragment:ocp-4.21-rhoai-3.5-ea.2-64185b995fe93f3ae9d014f1124714ba96b5"
    ),
}


class SnapshotCatalogLineTest(unittest.TestCase):
    def test_prname_225(self) -> None:
        self.assertEqual(
            catalog_line_from_prname("rhoai-fbc-fragment-rhoai-225-ocp-421-on-push"),
            "2.25",
        )

    def test_prname_35_ea2(self) -> None:
        self.assertEqual(
            catalog_line_from_prname("rhoai-fbc-fragment-rhoai-35-ea2-ocp-421-on-push"),
            "3.5-ea.2",
        )

    def test_snapshot_metadata_225(self) -> None:
        self.assertEqual(
            catalog_line_from_snapshot_metadata(_LABELS_225, _ANNOTATIONS_225),
            "2.25",
        )

    def test_snapshot_metadata_35_ea2(self) -> None:
        self.assertEqual(
            catalog_line_from_snapshot_metadata(_LABELS_35, _ANNOTATIONS_35),
            "3.5-ea.2",
        )

    def test_meets_min_version(self) -> None:
        self.assertFalse(catalog_line_meets_min_version("2.25", "3.5"))
        self.assertFalse(catalog_line_meets_min_version("3.3", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("3.5-ea.2", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("3.5", "3.5"))
        self.assertTrue(catalog_line_meets_min_version("", "3.5"))

    def test_cel_expression_catalog_path(self) -> None:
        cel = 'event == "push" && "catalog/rhoai-3.5-ea.2/v4.21/rhods-operator/catalog.yaml".pathChanged()'
        self.assertEqual(
            catalog_line_from_snapshot_metadata({}, {"pac.test.appstudio.openshift.io/on-cel-expression": cel}),
            "3.5-ea.2",
        )


if __name__ == "__main__":
    unittest.main()
