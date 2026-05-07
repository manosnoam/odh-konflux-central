"""Konflux olminstall CLI orchestration (watch, list, trigger snapshot)."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .constants import DEFAULT_KA_HOST, DEFAULT_KONFLUX_UI, KONFLUX_SERVER, PENDING_REASONS
from .errors import AppError
from .kubearchive import KubeArchiveClient
from .oc_util import filter_warning_lines, get_jsonpath, parse_json_output, run_cmd, ts_now


@dataclass
class PipelineRow:
    name: str
    app: str
    state: str
    created: str
    source: str


class OLMInstallRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_dir = Path(__file__).resolve().parent.parent
        self.snapshot_file = self.script_dir / "test-snapshot.yaml"
        self.its_file = self.script_dir / "its-olminstall-rhoai-tenant.yaml"
        self.konflux_ui = os.environ.get("KONFLUX_UI", DEFAULT_KONFLUX_UI)
        self.ka_host = os.environ.get("KA_HOST", DEFAULT_KA_HOST)
        self.pr_appear_timeout = int(os.environ.get("PR_APPEAR_TIMEOUT_SECONDS", "600"))
        self.cleanup_snapshot_on_exit = True
        self.snapshot_name = ""
        self.its_apply_tmp = ""
        self.log_file = ""
        self.pr = ""
        self.watch_completed = False
        self.watch_from_archive = False
        self.ka_succeeded = "Unknown"
        self.pipeline_exit = 0
        self.run_owner = ""
        self.token = ""
        self.ka: KubeArchiveClient | None = None
        self.resolved_app = ""
        self.image = args.image or ""
        self.update_channel_override = args.channel or ""

    def cleanup(self) -> None:
        if self.cleanup_snapshot_on_exit and self.snapshot_name:
            print("\n-- Cleaning up --")
            proc = run_cmd(
                ["oc", "delete", "snapshot", self.snapshot_name, "-n", self.args.namespace, "--ignore-not-found"],
                capture=True,
                check=False,
            )
            if proc.returncode == 0:
                print(f"  Deleted Snapshot {self.snapshot_name}")
        elif self.snapshot_name:
            print("\n-- Cleaning up --")
            print(f"  Keeping Snapshot {self.snapshot_name} for delayed trigger/debug")
        if self.its_apply_tmp and Path(self.its_apply_tmp).exists():
            Path(self.its_apply_tmp).unlink(missing_ok=True)
        if self.log_file and Path(self.log_file).exists():
            Path(self.log_file).unlink(missing_ok=True)

    def check_login(self) -> None:
        who = run_cmd(["oc", "whoami"], capture=True, check=False)
        if who.returncode != 0:
            raise AppError("Not logged in. Run: oc login --server=<api-url> --web")
        self.run_owner = who.stdout.strip()
        self.token = get_jsonpath(["oc", "whoami", "-t"])
        print(
            f"User: {self.run_owner}  Product: {self.args.product}  "
            f"Namespace: {self.args.namespace}  App: {self.args.app}"
        )
        self.ka = KubeArchiveClient(self.ka_host, self.token)

    def ka_available(self) -> bool:
        assert self.ka is not None
        ok = self.ka.check()
        if not ok:
            print(f"WARN KubeArchive API unreachable ({self.ka_host}); archived runs will not be shown.")
        return ok

    def get_pipelineruns(self, namespace: str, selector: str | None = None) -> list[dict[str, Any]]:
        cmd = ["oc", "get", "pipelineruns", "-n", namespace, "-o", "json"]
        if selector:
            cmd.extend(["-l", selector])
        data = parse_json_output(cmd)
        return data.get("items", []) if data else []

    def succeeded_condition(self, pr_name: str) -> tuple[str, str]:
        data = parse_json_output(["oc", "get", "pipelinerun", pr_name, "-n", self.args.namespace, "-o", "json"])
        for cond in data.get("status", {}).get("conditions", []):
            if cond.get("type") == "Succeeded":
                return cond.get("status", "Unknown"), cond.get("reason", "")
        return "Unknown", ""

    def list_pipelines(self) -> None:
        rows: list[PipelineRow] = []
        for item in self.get_pipelineruns(self.args.namespace):
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "-")
            if app != self.args.app:
                continue
            rows.append(
                PipelineRow(
                    name=item.get("metadata", {}).get("name", ""),
                    app=app,
                    state="completed" if item.get("status", {}).get("completionTime") else "running",
                    created=item.get("metadata", {}).get("creationTimestamp", ""),
                    source="live",
                )
            )
        rows.sort(key=lambda r: r.created, reverse=True)
        rows = rows[: self.args.list_pipelines]

        needed = self.args.list_pipelines - len(rows)
        if needed > 0 and self.ka_available():
            assert self.ka is not None
            limit = needed + self.args.list_pipelines
            path = (
                f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns"
                f"?labelSelector={quote(f'appstudio.openshift.io/application={self.args.app}')}&limit={limit}"
            )
            data = self.ka.get_json(path)
            for item in data.get("items", []):
                cond = next((c for c in item.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
                status = cond.get("status")
                if status == "True":
                    state = "completed"
                elif status == "False":
                    state = "failed"
                elif status:
                    state = "running"
                else:
                    state = "unknown"
                rows.append(
                    PipelineRow(
                        name=item.get("metadata", {}).get("name", ""),
                        app=item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "-"),
                        state=state,
                        created=item.get("metadata", {}).get("creationTimestamp", ""),
                        source="archived",
                    )
                )

        merged: list[PipelineRow] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda r: r.created, reverse=True):
            if row.name and row.name not in seen:
                merged.append(row)
                seen.add(row.name)
            if len(merged) >= self.args.list_pipelines:
                break

        print(f"Latest {self.args.list_pipelines} PipelineRuns for app '{self.args.app}' in namespace '{self.args.namespace}':")
        if not merged:
            print(f"No PipelineRuns found for app '{self.args.app}'.")
            print("Tip: use --app <name> to target another application.")
            return
        print("NAME\tAPP\tSTATE\tCREATED\tSOURCE")
        for r in merged:
            print(f"{r.name}\t{r.app}\t{r.state}\t{r.created}\t{r.source}")

    def get_snapshot_owner(self, snap: str) -> str:
        if not snap:
            return ""
        return get_jsonpath(
            [
                "oc",
                "get",
                "snapshot",
                snap,
                "-n",
                self.args.namespace,
                "-o",
                "jsonpath={.metadata.annotations.olminstall\\.run-owner}",
            ]
        )

    def find_owned_live_watch_pr(self) -> str:
        items = self.get_pipelineruns(self.args.namespace)
        cands = []
        for item in items:
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if "olminstall" not in name or app != self.args.app:
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "")
            cands.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner))
        for _, name, snap, owner in sorted(cands, reverse=True):
            if owner == self.run_owner or self.get_snapshot_owner(snap) == self.run_owner:
                return name
        return ""

    def find_owned_archived_watch_pr(self) -> str:
        if not self.ka_available():
            return ""
        assert self.ka is not None
        path = f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns?labelSelector={quote(f'appstudio.openshift.io/application={self.args.app}')}"
        items = self.ka.get_json(path).get("items", [])
        owned = [
            item
            for item in items
            if "olminstall" in item.get("metadata", {}).get("name", "")
            and item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "") == self.run_owner
        ]
        if not owned:
            return ""
        owned.sort(key=lambda i: i.get("metadata", {}).get("creationTimestamp", ""))
        return owned[-1].get("metadata", {}).get("name", "")

    def get_applications(self, namespace: str) -> list[str]:
        data = parse_json_output(["oc", "get", "applications", "-n", namespace, "-o", "json"])
        return [item.get("metadata", {}).get("name", "") for item in data.get("items", []) if item.get("metadata", {}).get("name")]

    def latest_matching_image(self, namespace: str, app_name: str, pattern: str) -> tuple[str, str]:
        data = parse_json_output(
            ["oc", "get", "snapshots", "-n", namespace, "-l", f"appstudio.openshift.io/application={app_name}", "-o", "json"]
        )
        best_ts = ""
        best_img = ""
        for item in data.get("items", []):
            ts = item.get("metadata", {}).get("creationTimestamp", "")
            for comp in item.get("spec", {}).get("components", []):
                img = comp.get("containerImage", "")
                if re.search(pattern, img):
                    if ts > best_ts:
                        best_ts = ts
                        best_img = img
        return best_ts, best_img

    def resolve_image(self, odh_overrides: bool) -> None:
        if self.image:
            print(f"Using provided image: {self.image}")
            return

        if self.args.product == "rhoai" and self.args.version:
            prefix = f"rhoai-v{self.args.version.replace('.', '-')}"
            print(f"Resolving latest FBCF image for RHOAI {self.args.version} (apps matching {prefix}*)...")
            apps = [a for a in self.get_applications("rhoai-tenant") if re.match(rf"^{re.escape(prefix)}(-|$)", a)]
            if not apps:
                raise AppError(f"No Konflux application found matching {prefix}* in rhoai-tenant")
            best_ts = ""
            for app in apps:
                ts, img = self.latest_matching_image("rhoai-tenant", app, r"rhoai-fbc-fragment@")
                if img and ts > best_ts:
                    best_ts = ts
                    self.image = img
                    self.resolved_app = app
            if not self.image:
                raise AppError(f"No FBCF snapshot found for RHOAI {self.args.version} (searched {prefix}*)")
            print(f"RHOAI {self.args.version} FBCF image: {self.image} (from {self.resolved_app})")
        elif self.args.product == "rhoai":
            print("Fetching latest FBCF image across all RHOAI apps (highest version)...")
            apps = [a for a in self.get_applications("rhoai-tenant") if a.startswith("rhoai-v")]
            best_ts = ""
            for app in apps:
                ts, img = self.latest_matching_image("rhoai-tenant", app, r"rhoai-fbc-fragment@")
                if img and ts > best_ts:
                    best_ts = ts
                    self.image = img
                    self.resolved_app = app
            if self.image:
                print(f"Latest FBCF image: {self.image} (from {self.resolved_app})")
            else:
                print("WARN Could not fetch latest image - falling back to pinned image in test-snapshot.yaml")
        elif self.args.product == "odh":
            repo = "quay.io/opendatahub/opendatahub-operator-catalog"
            tag = "odh-stable"
            print("Fetching latest ODH catalog snapshot from open-data-hub-tenant...")
            data = parse_json_output(["oc", "get", "snapshots", "-n", "open-data-hub-tenant", "-o", "json"])
            best_ts = ""
            for item in data.get("items", []):
                if item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application") != "opendatahub-builds":
                    continue
                ts = item.get("metadata", {}).get("creationTimestamp", "")
                for comp in item.get("spec", {}).get("components", []):
                    img = comp.get("containerImage", "")
                    if re.search(r"opendatahub-operator-catalog@|odh-operator-catalog@", img) and ts > best_ts:
                        best_ts = ts
                        self.image = img
            if not self.image:
                print("  No snapshots found (likely no access to open-data-hub-tenant)")
                print(f"  Resolving from {repo}:{tag} via skopeo...")
                if shutil.which("skopeo"):
                    out = parse_json_output(["skopeo", "inspect", "--no-tags", f"docker://{repo}:{tag}"])
                    digest = out.get("Digest", "")
                    if digest:
                        self.image = f"{repo}@{digest}"
                if not self.image:
                    print("  skopeo unavailable or inspect failed - using tag reference")
                    self.image = f"{repo}:{tag}"
            print(f"Latest ODH catalog image: {self.image}")

        if not self.update_channel_override and self.args.product == "odh":
            self.update_channel_override = "odh-stable"
            print(f"Auto-selected channel: {self.update_channel_override} (product={self.args.product})")
        elif not self.update_channel_override and self.resolved_app.startswith("rhoai-v3-"):
            self.update_channel_override = "stable-3.x"
            print(f"Auto-selected channel: {self.update_channel_override} (from {self.resolved_app})")

    def ensure_its_applied(self, odh_overrides: bool) -> None:
        need_yq = any([self.args.konflux_repo, self.args.konflux_branch, self.update_channel_override, odh_overrides])
        print("Ensuring IntegrationTestScenario is applied...")
        if not need_yq:
            proc = run_cmd(["oc", "apply", "-n", self.args.namespace, "-f", str(self.its_file)], capture=True, check=False)
            filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
            if filtered.strip():
                print(filtered, file=sys.stderr)
            if proc.returncode != 0:
                raise AppError("ITS apply failed")
            print("ITS ready")
            return

        if not shutil.which("yq"):
            raise AppError("yq is required for --konflux-repo / --konflux-branch / --channel / --product odh.")

        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.its_apply_tmp = tmp.name
        del_names: list[str] = []
        if self.args.konflux_repo:
            del_names.append("SCRIPTS_REPO_URL")
        if self.args.konflux_branch:
            del_names.append("SCRIPTS_REPO_REVISION")
        if self.update_channel_override:
            del_names.append("UPDATE_CHANNEL")
        if odh_overrides:
            del_names.extend(["OPERATOR_NAME", "OPERATOR_NAMESPACE", "FBCF_COMPONENT_NAME"])

        if del_names:
            expr = " or ".join(f'.name == "{n}"' for n in del_names)
            proc = run_cmd(["yq", "e", f"del(.spec.params[] | select({expr}))", str(self.its_file)], capture=True, check=True)
            Path(self.its_apply_tmp).write_text(proc.stdout, encoding="utf-8")
        else:
            shutil.copyfile(self.its_file, self.its_apply_tmp)

        if self.args.konflux_repo:
            run_cmd(
                ["bash", "-lc", f'YQ_SCRIPTS_URL="{self.args.konflux_repo}" yq e \'.spec.params += [{{"name":"SCRIPTS_REPO_URL","value":strenv(YQ_SCRIPTS_URL)}}]\' -i "{self.its_apply_tmp}"'],
                capture=True,
                check=True,
            )
            run_cmd(
                ["bash", "-lc", f'YQ_RESOLVER_URL="{self.args.konflux_repo}" yq e \'(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_RESOLVER_URL)\' -i "{self.its_apply_tmp}"'],
                capture=True,
                check=True,
            )
        if self.args.konflux_branch:
            run_cmd(
                ["bash", "-lc", f'YQ_SCRIPTS_REV="{self.args.konflux_branch}" yq e \'.spec.params += [{{"name":"SCRIPTS_REPO_REVISION","value":strenv(YQ_SCRIPTS_REV)}}]\' -i "{self.its_apply_tmp}"'],
                capture=True,
                check=True,
            )
            run_cmd(
                ["bash", "-lc", f'YQ_RESOLVER_REV="{self.args.konflux_branch}" yq e \'(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_RESOLVER_REV)\' -i "{self.its_apply_tmp}"'],
                capture=True,
                check=True,
            )
        if self.update_channel_override:
            run_cmd(
                ["bash", "-lc", f'YQ_UPDATE_CHANNEL="{self.update_channel_override}" yq e \'.spec.params += [{{"name":"UPDATE_CHANNEL","value":strenv(YQ_UPDATE_CHANNEL)}}]\' -i "{self.its_apply_tmp}"'],
                capture=True,
                check=True,
            )
        if odh_overrides:
            run_cmd(["yq", "e", '.spec.params += [{"name":"OPERATOR_NAME","value":"opendatahub-operator"}]', "-i", self.its_apply_tmp], capture=True, check=True)
            run_cmd(["yq", "e", '.spec.params += [{"name":"OPERATOR_NAMESPACE","value":"opendatahub-operators"}]', "-i", self.its_apply_tmp], capture=True, check=True)
            run_cmd(["yq", "e", '.spec.params += [{"name":"FBCF_COMPONENT_NAME","value":"odh-operator-catalog"}]', "-i", self.its_apply_tmp], capture=True, check=True)

        print(
            "  ITS overrides:"
            f" resolverRef={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" SCRIPTS_REPO={self.args.konflux_repo or '<default>'}@{self.args.konflux_branch or '<default>'}"
            f" UPDATE_CHANNEL={self.update_channel_override or '<pipeline default>'}"
            f" PRODUCT={self.args.product}"
        )
        proc = run_cmd(["oc", "apply", "-n", self.args.namespace, "-f", self.its_apply_tmp], capture=True, check=False)
        filtered = filter_warning_lines(f"{proc.stdout}\n{proc.stderr}")
        if filtered.strip():
            print(filtered, file=sys.stderr)
        if proc.returncode != 0:
            raise AppError("ITS apply failed")
        print("ITS ready")

    def create_snapshot(self, odh_overrides: bool) -> None:
        snap_yaml = self.snapshot_file.read_text(encoding="utf-8")
        snap_yaml = re.sub(r"(^\s*application:\s*).*$", rf"\1{self.args.app}", snap_yaml, flags=re.MULTILINE)
        if self.image:
            snap_yaml = re.sub(r"(^\s*containerImage:\s*).*$", rf"\1{self.image}", snap_yaml, flags=re.MULTILINE)
        if odh_overrides:
            snap_yaml = snap_yaml.replace("name: rhoai-fbc-fragment-ocp-421", "name: odh-operator-catalog")
        print(f"Creating Snapshot to trigger pipeline (app: {self.args.app})...")
        proc = run_cmd(
            ["oc", "create", "-n", self.args.namespace, "-f", "-", "-o", "jsonpath={.metadata.name}"],
            capture=True,
            check=True,
            input_text=snap_yaml,
        )
        self.snapshot_name = proc.stdout.strip()
        run_cmd(
            [
                "oc",
                "annotate",
                "snapshot",
                self.snapshot_name,
                "-n",
                self.args.namespace,
                f"olminstall.run-owner={self.run_owner}",
                "--overwrite",
            ],
            capture=True,
            check=False,
        )
        print(f"Snapshot: {self.snapshot_name}")
        print(f"  Snapshot owner marker: {self.run_owner}")

    def wait_for_pipelinerun(self) -> None:
        attempts = max(1, (self.pr_appear_timeout + 4) // 5)
        print(f"Waiting for PipelineRun to start (snapshot: {self.snapshot_name})...")
        for i in range(1, attempts + 1):
            items = self.get_pipelineruns(self.args.namespace)
            cands = []
            for item in items:
                name = item.get("metadata", {}).get("name", "")
                app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
                if "olminstall" not in name or app != self.args.app:
                    continue
                snap = ""
                for p in item.get("spec", {}).get("params", []):
                    if p.get("name") == "SNAPSHOT":
                        snap = str(p.get("value", ""))
                        break
                if snap == self.snapshot_name:
                    cands.append((item.get("metadata", {}).get("creationTimestamp", ""), name))
            if cands:
                cands.sort()
                self.pr = cands[-1][1]
                break

            active = []
            for item in items:
                name = item.get("metadata", {}).get("name", "")
                app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
                if "olminstall" in name and app == self.args.app and not item.get("status", {}).get("completionTime"):
                    active.append((item.get("metadata", {}).get("creationTimestamp", ""), name))
            if active:
                active.sort()
                self.pr = active[-1][1]
                print(f"No snapshot-linked run yet; attaching to active app run: {self.pr}")
                break

            print(f"  waiting... ({i}/{attempts})")
            time.sleep(5)

        if not self.pr:
            self.cleanup_snapshot_on_exit = False
            raise AppError(
                f"PipelineRun did not appear after {self.pr_appear_timeout}s. Check Konflux:\n"
                f"{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/activity/pipelineruns\n"
                "Tip: rerun the script in a minute; it will try to reattach first."
            )

        run_cmd(
            [
                "oc",
                "annotate",
                "pipelinerun",
                self.pr,
                "-n",
                self.args.namespace,
                f"olminstall.run-owner={self.run_owner}",
                "--overwrite",
            ],
            capture=True,
            check=False,
        )

    def ensure_konflux_cluster(self) -> None:
        res = run_cmd(["oc", "api-resources", "--api-group=appstudio.redhat.com"], capture=True, check=False)
        if "IntegrationTestScenario" in (res.stdout or ""):
            return
        print(f"\nWARN Current cluster ({get_jsonpath(['oc', 'whoami', '--show-server'])}) is not Konflux.")
        ans = "Y"
        if sys.stdin.isatty():
            ans = input(f"   Log in to {KONFLUX_SERVER} now? [Y/n] ") or "Y"
        if not ans.lower().startswith("y"):
            raise AppError("Aborting - not connected to a Konflux cluster.")
        run_cmd(["oc", "login", f"--server={KONFLUX_SERVER}", "--web"], capture=False, check=True)
        res2 = run_cmd(["oc", "api-resources", "--api-group=appstudio.redhat.com"], capture=True, check=False)
        if "IntegrationTestScenario" not in (res2.stdout or ""):
            raise AppError("Still no IntegrationTestScenario CRD after login. Aborting.")
        print(f"OK Re-logged in as {get_jsonpath(['oc', 'whoami'])} on Konflux cluster")

    def run_watch_mode(self) -> None:
        if self.args.watch:
            print(f"Watch mode: explicit PipelineRun '{self.args.watch}'")
            if run_cmd(["oc", "get", "pipelinerun", self.args.watch, "-n", self.args.namespace], capture=True, check=False).returncode == 0:
                self.pr = self.args.watch
            elif self.ka_available():
                assert self.ka is not None
                prj = self.ka.get_json(f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.args.watch)}")
                if prj.get("metadata", {}).get("name"):
                    self.pr = prj["metadata"]["name"]
                    self.watch_from_archive = True
                    self.watch_completed = True
                    print("Found PipelineRun in KubeArchive (pruned from live cluster).")
                else:
                    raise AppError(f"PipelineRun not found in namespace '{self.args.namespace}' or in KubeArchive: {self.args.watch}")
            else:
                raise AppError(f"PipelineRun not found in namespace '{self.args.namespace}': {self.args.watch}")
        else:
            print(f"Watch mode: looking for your latest owned olminstall PipelineRun (app: {self.args.app}, owner: {self.run_owner})...")
            self.pr = self.find_owned_live_watch_pr()
            if not self.pr:
                self.pr = self.find_owned_archived_watch_pr()
                if self.pr:
                    self.watch_from_archive = True
                    self.watch_completed = True
                    print(f"  Found archived owned PipelineRun: {self.pr}")
            if not self.pr:
                raise AppError(
                    f"No owned olminstall PipelineRun found for app '{self.args.app}' (live or archived).\n"
                    "Use '--watch <pipelinerun>' to target a specific run, or run with trigger flags (for example --product rhoai)."
                )
            if not self.watch_from_archive:
                print(f"Found latest owned PipelineRun: {self.pr}")

        if self.watch_from_archive:
            assert self.ka is not None
            prj = self.ka.get_json(f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}")
            cond = next((c for c in prj.get("status", {}).get("conditions", []) if c.get("type") == "Succeeded"), {})
            if cond.get("status") == "True":
                self.ka_succeeded = "Succeeded"
            elif cond.get("status") == "False":
                self.ka_succeeded = "Failed"
            else:
                self.ka_succeeded = "Unknown"
            ctime = prj.get("status", {}).get("completionTime", "")
            print(f"PipelineRun {self.pr} is archived ({self.ka_succeeded}, completionTime={ctime or '?'}). Replaying logs from KubeArchive.")
        else:
            ctime = get_jsonpath(
                ["oc", "get", "pipelinerun", self.pr, "-n", self.args.namespace, "-o", "jsonpath={.status.completionTime}"]
            )
            if ctime:
                self.watch_completed = True
                print(f"PipelineRun {self.pr} is already completed (completionTime={ctime}). Showing recent logs/status.")

    def run_trigger_mode(self) -> None:
        print(f"Checking for running olminstall PipelineRun (app: {self.args.app}, owner: {self.run_owner})...")
        active = []
        for item in self.get_pipelineruns(self.args.namespace):
            name = item.get("metadata", {}).get("name", "")
            app = item.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/application", "")
            if "olminstall" not in name or app != self.args.app:
                continue
            if item.get("status", {}).get("completionTime"):
                continue
            snap = ""
            for p in item.get("spec", {}).get("params", []):
                if p.get("name") == "SNAPSHOT":
                    snap = str(p.get("value", ""))
                    break
            owner = item.get("metadata", {}).get("annotations", {}).get("olminstall.run-owner", "")
            active.append((item.get("metadata", {}).get("creationTimestamp", ""), name, snap, owner))
        active.sort(reverse=True)
        fallback = active[0][1] if active else ""
        self.pr = ""
        for _, name, snap, owner in active:
            if owner == self.run_owner or self.get_snapshot_owner(snap) == self.run_owner:
                self.pr = name
                break
        if self.pr:
            print(f"Found running PipelineRun for app '{self.args.app}' owned by '{self.run_owner}': {self.pr} - attaching...")
            return
        if fallback:
            self.pr = fallback
            print(f"Found running PipelineRun for app '{self.args.app}' but owner marker is unavailable - attaching to latest: {self.pr}")
            return
        odh_overrides = self.args.product == "odh"
        self.resolve_image(odh_overrides)
        self.ensure_its_applied(odh_overrides)
        self.create_snapshot(odh_overrides)
        self.wait_for_pipelinerun()

    def replay_archived_logs(self) -> None:
        assert self.ka is not None
        prj = self.ka.get_json(f"/apis/tekton.dev/v1/namespaces/{quote(self.args.namespace)}/pipelineruns/{quote(self.pr)}")
        refs = prj.get("status", {}).get("childReferences", [])
        if not refs:
            print("(no child TaskRuns found in archived PipelineRun)")
            return
        for ref in refs:
            tr_name = ref.get("name", "")
            task_name = ref.get("pipelineTaskName", tr_name)
            pods = self.ka.get_json(
                f"/api/v1/namespaces/{quote(self.args.namespace)}/pods?labelSelector={quote(f'tekton.dev/taskRun={tr_name}')}"
            )
            items = pods.get("items", [])
            if not items:
                print(f"[{task_name}] (no pod found)")
                continue
            pod = items[0].get("metadata", {}).get("name", "")
            pod_obj = self.ka.get_json(f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}")
            containers = [c.get("name", "") for c in pod_obj.get("spec", {}).get("initContainers", [])] + [
                c.get("name", "") for c in pod_obj.get("spec", {}).get("containers", [])
            ]
            for ctr in containers:
                if ctr in {"prepare", "place-scripts", "place-tools"}:
                    continue
                print(f"\n[{task_name} : {ctr}]")
                print(self.ka.get_text(f"/api/v1/namespaces/{quote(self.args.namespace)}/pods/{quote(pod)}/log?container={quote(ctr)}"), end="")

    def stream_live_logs(self) -> None:
        self.log_file = tempfile.mktemp(prefix="olminstall-run.")
        if shutil.which("tkn"):
            if self.watch_completed:
                print("Pipeline is already finished - showing last 200 log lines via tkn...")
                # tkn often writes log output to stderr; merge into stdout for parsing.
                lines: list[str] = []
                last_rc = 0
                for extra in ([], ["-a"]):
                    cmd = ["tkn", "pipelinerun", "logs", self.pr, "-n", self.args.namespace, *extra]
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    last_rc = proc.returncode
                    lines = (proc.stdout or "").splitlines()
                    if lines:
                        break
                if len(lines) > 200:
                    lines = lines[-200:]
                if not lines:
                    print(
                        f"WARN No log lines from tkn (exit {last_rc}). Try manually:\n"
                        f"  tkn pipelinerun logs {self.pr} -n {self.args.namespace}"
                    )
                else:
                    with open(self.log_file, "w", encoding="utf-8") as fh:
                        for line in lines:
                            out = f"[{ts_now()}] {line}"
                            print(out)
                            fh.write(out + "\n")
                return
            print("Streaming logs via tkn (Ctrl-C to detach, pipeline keeps running)...")
            with open(self.log_file, "w", encoding="utf-8") as fh:
                p = subprocess.Popen(
                    ["tkn", "pipelinerun", "logs", self.pr, "-n", self.args.namespace, "-f"],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                assert p.stdout is not None
                for line in p.stdout:
                    out = f"[{ts_now()}] {line.rstrip()}"
                    print(out)
                    fh.write(out + "\n")
            return
        print("tkn not found - polling status (install tkn for live logs)")
        deadline = time.time() + 5400
        while time.time() < deadline:
            cstat, reason = self.succeeded_condition(self.pr)
            print(f"  {ts_now()}  succeeded-condition: {cstat}  reason: {reason or '?'}")
            if cstat == "True":
                print("Pipeline succeeded")
                return
            if cstat == "False":
                self.pipeline_exit = 1
                print(f"Pipeline failed ({reason or 'Failed'})")
                return
            time.sleep(15)
        self.pipeline_exit = 1
        raise AppError("Polling timed out before pipeline reached a terminal state")

    def print_summary(self, final_status: str) -> None:
        op_ver = ""
        if self.log_file and Path(self.log_file).exists():
            txt = Path(self.log_file).read_text(encoding="utf-8", errors="ignore")
            m = re.findall(r"Operator version\s*:\s*([^\s]+)", txt)
            op_ver = m[-1] if m else ""
        print("\n===========================================================")
        print(" Summary")
        print("===========================================================")
        print(f"  Pipeline  : {self.pr}  [{final_status or 'unknown'}]")
        if op_ver:
            print(f"  Operator  : {op_ver}")
        print(f"  Konflux UI: {self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/pipelineruns/{self.pr}")
        print("===========================================================")

    def run(self) -> int:
        self.check_login()

        if self.args.list_pipelines:
            self.list_pipelines()
            return 0

        self.ensure_konflux_cluster()

        if self.args.watch_mode:
            self.run_watch_mode()
        else:
            self.run_trigger_mode()

        print("")
        print(f"PipelineRun : {self.pr}")
        if self.watch_from_archive:
            print("Source      : KubeArchive (pruned from live cluster)")
        else:
            print(f"Logs        : tkn pipelinerun logs {self.pr} -n {self.args.namespace} -f")
        print(f"Konflux UI  : {self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/pipelineruns/{self.pr}")
        print("")

        if self.watch_from_archive:
            self.log_file = tempfile.mktemp(prefix="olminstall-run.")
            print("Replaying archived logs from KubeArchive...")
            with open(self.log_file, "w", encoding="utf-8") as fh:
                old_stdout = sys.stdout
                try:
                    sys.stdout = Tee(sys.stdout, fh)
                    self.replay_archived_logs()
                finally:
                    sys.stdout = old_stdout
            self.print_summary(self.ka_succeeded)
            if self.ka_succeeded == "Failed":
                self.pipeline_exit = 1
            return self.pipeline_exit

        if not self.watch_completed:
            wait_deadline = time.time() + 300
            wait_start = time.time()
            print("Waiting for pipeline to start running...")
            while time.time() < wait_deadline:
                _, reason = self.succeeded_condition(self.pr)
                if reason in PENDING_REASONS:
                    elapsed = int(time.time() - wait_start)
                    print(f"  {ts_now()}  {reason or 'pending'} ({elapsed}s)")
                    time.sleep(10)
                    continue
                print(f"  {ts_now()}  {reason or 'starting'} - ready to stream")
                break
            else:
                self.pipeline_exit = 1
                raise AppError(f"Pipeline still pending after 5m. Check Konflux:\n{self.konflux_ui}/ns/{self.args.namespace}/applications/{self.args.app}/pipelineruns/{self.pr}")

        self.stream_live_logs()
        final_cstat, final_reason = self.succeeded_condition(self.pr)
        self.print_summary(final_reason)
        if final_cstat != "True":
            self.pipeline_exit = 1
        return self.pipeline_exit


class Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()
