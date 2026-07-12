"""Apply or remove IntegrationTestScenario objects on the Konflux cluster."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from k8s.oc_util import filter_warning_lines, run_cmd
from suite.errors import AppError
from suite.its_registry import (
    integration_test_scenario_application,
    resolve_integration_test_scenario_manifest,
    resolve_integration_test_scenario_run_now_snapshot,
    validate_integration_test_scenario_name,
)
from suite.its_trigger_params import CLUSTER_SOURCE_EAAS, is_external_cluster_source
from suite.pipelinerun_naming import build_olminstall_generate_prefix
from .runner_support import format_olm_pipeline_watch_cli


class RunnerItsAdminMixin:
    def enable_integration_test_scenario(self) -> int:
        name = validate_integration_test_scenario_name(self.args.enable_its)
        self._apply_integration_test_scenario(name)
        return 0

    def enable_integration_test_scenario_run_now(self) -> int:
        """One-shot run: direct PipelineRun with ITS manifest params and dynamic generateName."""
        name = validate_integration_test_scenario_name(self.args.enable_its)
        manifest = resolve_integration_test_scenario_manifest(self.script_dir, name)
        snap_path = resolve_integration_test_scenario_run_now_snapshot(self.script_dir, name)
        self._stage_its_manifest_tmp(manifest, push_context=False)
        self.snapshot_file = snap_path
        self._apply_run_now_manifest_defaults(manifest)
        self._apply_konflux_git_inference_from_clone_or_env()
        odh_overrides = self.args.product == "odh"
        items_by_name = {
            item.get("metadata", {}).get("name", ""): item
            for item in self.get_pipelineruns(self.args.namespace)
            if item.get("metadata", {}).get("name")
        }
        owned_running = self.find_owned_live_watch_pr()
        self._guard_external_cluster_before_trigger(
            owned_running=owned_running,
            items_by_name=items_by_name,
        )
        self.resolve_image(odh_overrides)
        self._pipelinerun_generate_prefix = self._run_now_generate_prefix(manifest)
        print(f"  pipelinerun_prefix={self._pipelinerun_generate_prefix!r}")
        self.create_direct_pipelinerun(odh_overrides)
        watch_hint = format_olm_pipeline_watch_cli(
            olminstall_dir=self.script_dir,
            namespace=self.args.namespace,
            app=self.args.app,
            pipelinerun=self.pr or "",
        )
        print(f"Watch the run with:\n  {watch_hint}")
        return 0

    def disable_integration_test_scenario(self) -> int:
        name = validate_integration_test_scenario_name(self.args.disable_its)
        self._remove_integration_test_scenario(name)
        return 0

    @staticmethod
    def _its_manifest_param(manifest: Path, param_name: str) -> str:
        try:
            import yaml
        except ImportError as exc:
            raise AppError("PyYAML is required to read ITS manifests.", 1) from exc
        doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return ""
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            return ""
        params = spec.get("params")
        if not isinstance(params, list):
            return ""
        for item in params:
            if isinstance(item, dict) and item.get("name") == param_name:
                return str(item.get("value", "")).strip()
        return ""

    def _apply_run_now_manifest_defaults(self, manifest: Path) -> None:
        product = self._its_manifest_param(manifest, "PRODUCT")
        if product:
            self.args.product = product
        tests = self._its_manifest_param(manifest, "TEST_GATES")
        if tests:
            self.args.tests = tests
        fbc_name = self._its_manifest_param(manifest, "RHOAI_FBC_NAME")
        if fbc_name:
            self.resolved_rhoai_fbc_name = fbc_name
        cluster_source = self._its_manifest_param(manifest, "CLUSTER_SOURCE")
        if is_external_cluster_source(cluster_source):
            self.external_kubeconfig_secret = cluster_source
        ocp_version = self._its_manifest_param(manifest, "OCP_VERSION")
        if ocp_version and not (getattr(self.args, "ocp_version", "") or "").strip():
            self.args.ocp_version = ocp_version
        # Konflux lookup in resolve_image(); snapshot pin is offline fallback only.
        self._run_now_pinned_fbcf_image = self._snapshot_yaml_container_image()

    def _run_now_generate_prefix(self, manifest: Path) -> str:
        cluster_source = self._its_manifest_param(manifest, "CLUSTER_SOURCE")
        cluster_label = ""
        target_type = "stub"
        if is_external_cluster_source(cluster_source):
            target_type = "external"
            if cluster_source:
                cluster_label = self._cluster_label_for_external_secret(cluster_source)
        elif cluster_source == CLUSTER_SOURCE_EAAS:
            target_type = "eaas"
        return build_olminstall_generate_prefix(
            product=self._its_manifest_param(manifest, "PRODUCT") or self.args.product,
            version=self._its_manifest_param(manifest, "RHOAI_VERSION"),
            cluster_source=cluster_source,
            cluster_label=cluster_label,
            target_type=target_type,
            tests_csv=self._its_manifest_param(manifest, "TEST_GATES") or self.args.tests,
            run_owner=self.run_owner,
        )

    def _stage_its_manifest_tmp(self, manifest: Path, *, push_context: bool) -> None:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        shutil.copyfile(manifest, tmp_path)
        if push_context:
            run_cmd(
                [
                    "yq",
                    "e",
                    '.spec.contexts = [{"name": "push", "description": "Manual Snapshot (--run-now)"}]',
                    "-i",
                    str(tmp_path),
                ],
                capture=True,
                check=True,
            )
        konflux_repo = (getattr(self.args, "konflux_repo", "") or "").strip()
        konflux_branch = (getattr(self.args, "konflux_branch", "") or "").strip()
        if konflux_repo:
            run_cmd(
                [
                    "yq",
                    "e",
                    '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_KONFLUX_REPO)',
                    "-i",
                    str(tmp_path),
                ],
                capture=True,
                check=True,
                env={**os.environ, "YQ_KONFLUX_REPO": konflux_repo},
            )
            self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_URL", konflux_repo)
        if konflux_branch:
            run_cmd(
                [
                    "yq",
                    "e",
                    '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_KONFLUX_BRANCH)',
                    "-i",
                    str(tmp_path),
                ],
                capture=True,
                check=True,
                env={**os.environ, "YQ_KONFLUX_BRANCH": konflux_branch},
            )
            self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_REVISION", konflux_branch)
        self.its_apply_tmp = str(tmp_path)

    def _apply_integration_test_scenario(
        self, name: str, *, param_overrides: dict[str, str] | None = None
    ) -> None:
        manifest = resolve_integration_test_scenario_manifest(self.script_dir, name)
        manifest_app = integration_test_scenario_application(manifest)
        if manifest_app and manifest_app != self.args.app:
            raise AppError(
                f"ITS {name!r} targets application {manifest_app!r}, not "
                f"--konflux-app {self.args.app!r}.",
                2,
            )
        konflux_repo = (getattr(self.args, "konflux_repo", "") or "").strip()
        konflux_branch = (getattr(self.args, "konflux_branch", "") or "").strip()
        apply_path = manifest
        tmp_path: Path | None = None
        if konflux_repo or konflux_branch or param_overrides:
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
            tmp_path = Path(tmp.name)
            tmp.close()
            shutil.copyfile(manifest, tmp_path)
            if konflux_repo:
                run_cmd(
                    [
                        "yq",
                        "e",
                        '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_KONFLUX_REPO)',
                        "-i",
                        str(tmp_path),
                    ],
                    capture=True,
                    check=True,
                    env={**os.environ, "YQ_KONFLUX_REPO": konflux_repo},
                )
                self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_URL", konflux_repo)
            if konflux_branch:
                run_cmd(
                    [
                        "yq",
                        "e",
                        '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_KONFLUX_BRANCH)',
                        "-i",
                        str(tmp_path),
                    ],
                    capture=True,
                    check=True,
                    env={**os.environ, "YQ_KONFLUX_BRANCH": konflux_branch},
                )
                self._yq_upsert_its_param(tmp_path, "SCRIPTS_REPO_REVISION", konflux_branch)
            if param_overrides:
                for param_name, param_value in param_overrides.items():
                    self._yq_upsert_its_param(tmp_path, param_name, param_value)
            apply_path = tmp_path
        print(
            f"Applying IntegrationTestScenario {name!r} to namespace {self.args.namespace!r} "
            f"(application {manifest_app or self.args.app!r})..."
        )
        try:
            proc = run_cmd(
                ["oc", "apply", "-n", self.args.namespace, "-f", str(apply_path)],
                capture=True,
                check=False,
            )
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered, file=sys.stderr)
        if proc.returncode != 0:
            raise AppError(f"oc apply failed for IntegrationTestScenario {name!r}.", 1)
        print(f"IntegrationTestScenario {name!r} enabled.")

    def _remove_integration_test_scenario(self, name: str) -> None:
        print(
            f"Deleting IntegrationTestScenario {name!r} from namespace {self.args.namespace!r} "
            f"(application {self.args.app!r})..."
        )
        proc = run_cmd(
            [
                "oc",
                "delete",
                "integrationtestscenario",
                name,
                "-n",
                self.args.namespace,
                "--ignore-not-found",
            ],
            capture=True,
            check=False,
        )
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered)
        if proc.returncode != 0:
            raise AppError(f"oc delete failed for IntegrationTestScenario {name!r}.", 1)
        print(f"IntegrationTestScenario {name!r} disabled (removed from cluster).")

    @staticmethod
    def _yq_upsert_its_param(path: Path, name: str, value: str) -> None:
        env_key = f"YQ_{name}"
        run_cmd(
            ["yq", "e", f'del(.spec.params[] | select(.name == "{name}"))', "-i", str(path)],
            capture=True,
            check=True,
        )
        run_cmd(
            [
                "yq",
                "e",
                f'.spec.params += [{{"name":"{name}","value":strenv({env_key})}}]',
                "-i",
                str(path),
            ],
            capture=True,
            check=True,
            env={**os.environ, env_key: value},
        )
