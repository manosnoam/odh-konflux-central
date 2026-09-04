"""Unit tests for MaaS database infra cleanup on external pooled clusters."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from components.maas_billing.database import (
    _needs_maas_postgres_reset,
    cleanup_maas_database_infra,
    ensure_maas_database,
)


class MaasDatabaseCleanupTest(unittest.TestCase):
    @patch("components.maas_billing.database._delete_namespace_if_present")
    @patch("components.maas_billing.database._delete_maas_db_secrets")
    def test_cleanup_removes_secrets_and_namespaces(
        self,
        delete_secrets,
        delete_ns,
    ) -> None:
        cleanup_maas_database_infra()
        delete_secrets.assert_called_once_with()
        self.assertEqual(delete_ns.call_count, 2)

    @patch("components.maas_billing.database.maas_api_deployment_exists", return_value=False)
    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=None)
    @patch("components.maas_billing.database._maas_postgres_has_missing_schema", return_value=True)
    def test_skips_reset_when_schema_missing_before_maas_api_exists(
        self,
        _missing_schema,
        _schema,
        _api_ready,
        _api_exists,
    ) -> None:
        self.assertFalse(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database.maas_api_deployment_exists", return_value=True)
    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=None)
    @patch("components.maas_billing.database._maas_postgres_has_missing_schema", return_value=True)
    def test_needs_reset_when_schema_table_missing_and_api_exists(
        self,
        _missing_schema,
        _schema,
        _api_ready,
        _api_exists,
    ) -> None:
        self.assertTrue(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=False)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=5)
    def test_needs_reset_when_schema_present_and_api_not_ready(
        self,
        _schema,
        _api_ready,
    ) -> None:
        self.assertTrue(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._maas_api_deployment_ready", return_value=True)
    @patch("components.maas_billing.database._read_maas_postgres_schema_version", return_value=5)
    def test_skips_reset_when_api_ready(
        self,
        _schema,
        _api_ready,
    ) -> None:
        self.assertFalse(_needs_maas_postgres_reset())

    @patch("components.maas_billing.database._restart_maas_api_after_db_config")
    @patch("components.maas_billing.database.cleanup_maas_database_infra")
    @patch("components.maas_billing.database._needs_maas_postgres_reset", return_value=True)
    @patch("components.maas_billing.database._repair_apps_maas_db_connection_url_if_needed", return_value=False)
    @patch("components.maas_billing.database._secret_exists", return_value=True)
    @patch("components.maas_billing.database._namespace_exists", return_value=True)
    @patch("components.maas_billing.database._apps_namespace_ready_for_secrets", return_value=True)
    @patch("components.maas_billing.database._clone_models_as_a_service")
    @patch("components.maas_billing.database.subprocess.run")
    def test_ensure_resets_stale_schema_and_reruns_setup(
        self,
        subprocess_run,
        clone_repo,
        _apps_ns_ready,
        _ns_exists,
        secret_exists,
        _repair,
        needs_reset,
        cleanup,
        restart_api,
    ) -> None:
        from pathlib import Path

        repo = Path("/tmp/fake-models-as-a-service")
        clone_repo.return_value = repo
        subprocess_run.return_value.returncode = 0
        secret_exists.side_effect = [True, False, True]

        with patch.object(Path, "is_file", return_value=True):
            with patch(
                "components.maas_billing.database._promote_maas_db_secret_to_apps_namespace",
                return_value=True,
            ):
                ensure_maas_database()

        cleanup.assert_called_once_with()
        needs_reset.assert_called_once_with()
        restart_api.assert_called_once()


if __name__ == "__main__":
    unittest.main()
