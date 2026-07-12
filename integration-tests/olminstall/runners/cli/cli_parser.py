"""Argument parser definitions for ``olm_pipeline.py``."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import NoReturn

from suite.constants import (
    DEFAULT_APP,
    DEFAULT_KA_HOST,
    DEFAULT_KONFLUX_SERVER,
    DEFAULT_KONFLUX_UI,
    DEFAULT_LIST_COUNT,
    DEFAULT_NAMESPACE,
    DEFAULT_PRODUCT,
    DEFAULT_TESTS_CONFIG_RELATIVE,
    LIST_SUPPORTED_OCP_MAX_PRS,
    PRODUCT_CHOICES,
)

# When user passes ``--ka-host`` with no URL, read KA_HOST from the environment.
_KA_HOST_FROM_ENV = "__KA_HOST_FROM_ENV__"


class CliHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Keep epilog layout; append option defaults when helpful (similar intent to Click ``show_default``)."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_txt = action.help
        if help_txt is None:
            help_txt = ""
        if "%(default)" in help_txt:
            return super()._get_help_string(action)
        # PY-CLI-4: omit noisy "(default: False)" on store_true / store_false flags.
        if isinstance(action, argparse._StoreTrueAction) or isinstance(action, argparse._StoreFalseAction):
            return help_txt
        optional_value = action.nargs in (None, argparse.OPTIONAL, argparse.ZERO_OR_MORE)
        if (
            action.option_strings
            and optional_value
            and not action.required
            and action.default is not argparse.SUPPRESS
        ):
            if action.default == "":
                return help_txt
            if action.default is None:
                return help_txt
        return super()._get_help_string(action)


def emit_click_style_error(parser: argparse.ArgumentParser | None, message: str, *, usage: bool) -> None:
    if usage and parser is not None:
        parser.print_usage(sys.stderr)
        print(file=sys.stderr)
        print(f"Try '{parser.prog} --help' for help.\n", file=sys.stderr)
    print(f"Error: {message}", file=sys.stderr)


class CliArgumentParser(argparse.ArgumentParser):
    """Emit usage + ``Try '… --help'`` + ``Error:`` on parser failures (Click-style)."""

    def error(self, message: str) -> NoReturn:
        emit_click_style_error(self, message, usage=True)
        self.exit(2)


def _add_product_group(parser: CliArgumentParser) -> None:
    product = parser.add_argument_group(
        "product & catalog",
        "RHOAI/ODH deploy: product, FBC image, channel, EaaS OCP version, and supported-OCP query (trigger or --list-supported-ocp).",
    )
    product.add_argument(
        "--image",
        default="",
        metavar="REF",
        help="FBC/catalog image. Empty = resolve from Konflux for --product rhoai/odh; omitted for existing.",
    )
    product.add_argument(
        "--product",
        default=DEFAULT_PRODUCT,
        choices=PRODUCT_CHOICES,
        help=(
            "existing: tests on a cluster with RHOAI already installed (skip EaaS/install and FBC "
            "snapshot extract). rhoai or odh: catalog wiring and auto image resolution for full installs."
        ),
    )
    product.add_argument(
        "--install-dependencies",
        action="store_true",
        help=(
            "With --product existing: run install-dep-operators (setup-dependencies.sh, RHCL, "
            "cluster prep) before component tests instead of prepare-components-prerequisites."
        ),
    )
    product.add_argument(
        "--rhoai-version",
        dest="version",
        metavar="VER",
        default="",
        help="RHOAI only: resolve FBC image by rhoai-v* Application label.",
    )
    product.add_argument(
        "--channel",
        metavar="NAME",
        default="",
        help="OLM UPDATE_CHANNEL passed to the ITS (e.g. stable-3.x, odh-stable).",
    )
    product.add_argument(
        "--ocp-version",
        metavar="X.Y",
        default="",
        help=(
            "OCP cluster minor (e.g. 4.21): EaaS provisions that version; with --product rhoai selects "
            "rhoai-fbc-fragment-ocp-4XX from Konflux. External kubeconfig: optional override "
            "(auto-detected when omitted). With --list-supported-ocp, assert minor is listed."
        ),
    )
    product.add_argument(
        "--list-supported-ocp",
        action="store_true",
        help=f"Print supported OCP minors from archived logs (≤{LIST_SUPPORTED_OCP_MAX_PRS} runs). Use with --ocp-version to verify.",
    )


def _add_tests_group(parser: CliArgumentParser) -> None:
    tests = parser.add_argument_group(
        "tests & components",
        "Test gates, phase config, smoke components, and timeouts (trigger only).",
    )
    tests.add_argument(
        "--tests",
        metavar="LIST",
        default=None,
        help=(
            "Comma-separated TEST_GATES for the ITS (e.g. bvt,smoke). "
            "Unknown tokens that match catalog test slices (e.g. SmokeSet5, @SanitySet1) "
            "filter sub-selections and infer the matching phase (smoke/tier1). "
            "Include every phase marked requiredInSelection in --tests-config. "
            "Omit to use defaults from that file."
        ),
    )
    tests.add_argument(
        "--tests-config",
        metavar="PATH",
        default="",
        help=(
            "Path to olminstall-tests-config.yaml (phase list + defaults). "
            f"Default: {DEFAULT_TESTS_CONFIG_RELATIVE}"
        ),
    )
    tests.add_argument(
        "--tests-rhoai-version",
        dest="tests_rhoai_version",
        metavar="VER",
        default="",
        help=(
            "Override installed CSV for opendatahub-tests image tag and component version gates. "
            "Use with --product existing on external clusters (optional)."
        ),
    )
    tests.add_argument(
        "--components",
        metavar="LIST",
        default=None,
        help=(
            "Comma-separated smoke component ids (olminstall-components-smoke.yaml). "
            "Only when --tests includes smoke. Omit = all catalog components."
        ),
    )
    tests.add_argument(
        "--test-timeout",
        metavar="DURATION",
        default=os.environ.get("OLMINSTALL_TEST_TIMEOUT", ""),
        help=(
            "Per-component smoke pytest timeout (e.g. 10m, 90s). "
            "Failed components do not stop the pipeline. Env: OLMINSTALL_TEST_TIMEOUT."
        ),
    )


def _add_external_group(parser: CliArgumentParser) -> None:
    external = parser.add_argument_group(
        "external cluster",
        "Skip EaaS; run install/BVT/smoke on a pre-existing cluster (trigger only).",
    )
    external.add_argument(
        "--external-kubeconfig",
        metavar="PATH",
        default=os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG", ""),
        help="Upload local kubeconfig as a tenant Secret (key kubeconfig). Env: OLMINSTALL_EXTERNAL_KUBECONFIG.",
    )
    external.add_argument(
        "--external-kubeconfig-secret",
        metavar="NAME",
        default=os.environ.get("OLMINSTALL_EXTERNAL_KUBECONFIG_SECRET", ""),
        help="Use existing tenant Secret (key kubeconfig). Env: OLMINSTALL_EXTERNAL_KUBECONFIG_SECRET.",
    )
    external.add_argument(
        "--cleanup",
        action="store_true",
        help=(
            "Set CLEANUP=true: run olminstall cleanup.sh -t operator on the external cluster before "
            "install (requires --external-kubeconfig or --external-kubeconfig-secret). "
            "Destructive; disposable clusters only."
        ),
    )
    external.add_argument(
        "--force-cluster-run",
        action="store_true",
        help=(
            "Skip external-cluster single-flight wait/check and allow parallel olminstall runs on "
            "the same physical cluster (EAAS unchanged). Default: wait until the cluster is idle."
        ),
    )


def _add_konflux_group(parser: CliArgumentParser) -> None:
    konflux = parser.add_argument_group(
        "konflux",
        "PipelineRun control, tenant, Application, ITS enable/disable, pipeline git source, "
        "UI/API, and KubeArchive. Default (no run flag): trigger a new olminstall PipelineRun.",
    )
    konflux.add_argument(
        "--watch-pipelines",
        "-w",
        "--watch",
        nargs="?",
        const="",
        default=None,
        dest="watch",
        metavar="PIPELINERUN",
        help=(
            "Watch an existing run: newest olminstall PipelineRun for --konflux-app "
            "(same order as --list-pipelines), else match by owner/Snapshot, or name PIPELINERUN."
        ),
    )
    konflux.add_argument(
        "--list-pipelines",
        "-l",
        "--list",
        nargs="?",
        const=str(DEFAULT_LIST_COUNT),
        default=None,
        dest="list_pipelines",
        metavar="N",
        help=f"List last N olminstall PipelineRuns for --konflux-app (default N={DEFAULT_LIST_COUNT}).",
    )
    konflux.add_argument(
        "--delete-pending-pipelines",
        action="store_true",
        help=(
            "Stop incomplete olminstall PipelineRuns for --konflux-app: Kueue/resolver pending and your "
            "owned incomplete runs (PR or Snapshot olminstall.run-owner). Live runs with tasks are "
            "cancelled via tkn (Konflux Stop/Cancel) before oc delete when selected. Use "
            "--include-unowned-stuck for shared-tenant runs stuck with no TaskRuns. Does not remove "
            "archived Konflux UI ghosts (see README)."
        ),
    )
    konflux.add_argument(
        "--delete-pending-dry-run",
        action="store_true",
        help="With --delete-pending-pipelines: list targets only; no cancel or delete.",
    )
    konflux.add_argument(
        "--stop-owned-running",
        action="store_true",
        help=(
            "With --delete-pending-pipelines: also cancel+delete your owned PipelineRuns that are actively "
            "Running with TaskRuns (default skips them). Requires tkn in PATH for graceful cancel."
        ),
    )
    konflux.add_argument(
        "--include-unowned-stuck",
        action="store_true",
        help=(
            "With --delete-pending-pipelines: also stop olminstall runs stuck with no TaskRuns that lack "
            "your olminstall.run-owner marker (shared tenant only; default skips unowned runs)."
        ),
    )
    konflux.add_argument(
        "--konflux-namespace",
        dest="namespace",
        default=DEFAULT_NAMESPACE,
        metavar="NAMESPACE",
        help="Konflux tenant namespace.",
    )
    konflux.add_argument(
        "--konflux-app",
        dest="app",
        default=DEFAULT_APP,
        metavar="APP",
        help="Konflux Application name.",
    )
    konflux.add_argument(
        "--enable-its",
        metavar="NAME",
        default="",
        help=(
            "Apply an in-tree IntegrationTestScenario manifest by metadata.name "
            "(under tekton/its/). Uses --konflux-namespace and --konflux-app; "
            "manifest spec.application must match --konflux-app when set. "
            "Rh-nightly ITS: use --konflux-app rhoai-fbc-fragment-ocp-420. "
            "With --run-now: one direct PipelineRun from ITS params (does not apply ITS)."
        ),
    )
    konflux.add_argument(
        "--disable-its",
        metavar="NAME",
        default="",
        help=(
            "Delete IntegrationTestScenario NAME from --konflux-namespace "
            "(stops auto/integration triggers for that scenario)."
        ),
    )
    konflux.add_argument(
        "--run-now",
        action="store_true",
        help=(
            "With --enable-its only: create one direct PipelineRun from the ITS manifest "
            "(descriptive generateName; does not apply the ITS to the cluster)."
        ),
    )
    konflux.add_argument(
        "--konflux-ui",
        metavar="URL",
        default=os.environ.get("KONFLUX_UI", DEFAULT_KONFLUX_UI),
        help="Konflux UI base URL (env KONFLUX_UI; inferred on hosted clusters).",
    )
    konflux.add_argument(
        "--ka-host",
        nargs="?",
        metavar="URL",
        const=_KA_HOST_FROM_ENV,
        default=os.environ.get("KA_HOST", DEFAULT_KA_HOST),
        help="KubeArchive API base URL (archive UI). Bare --ka-host reads env KA_HOST.",
    )
    konflux.add_argument(
        "--konflux-server",
        metavar="URL",
        default=os.environ.get("KONFLUX_SERVER", DEFAULT_KONFLUX_SERVER),
        help="Konflux API URL for oc login fallback (env KONFLUX_SERVER).",
    )
    konflux.add_argument(
        "--konflux-repo",
        metavar="URL",
        default="",
        help=(
            "Git URL with integration-tests/olminstall/ (patches ITS resolver; needs yq). "
            "Omit = ITS default opendatahub-io/odh-konflux-central @ main. "
            "Or set OLMINSTALL_PIPELINE_REPO / OLMINSTALL_PIPELINE_REVISION."
        ),
    )
    konflux.add_argument(
        "--konflux-branch",
        metavar="REF",
        default="",
        help="Git revision for --konflux-repo (branch, tag, or SHA). Omit = main from ITS YAML.",
    )


def _add_reporting_group(parser: CliArgumentParser) -> None:
    reporting = parser.add_argument_group(
        "reporting",
        "Run notifications and external reporting (trigger only).",
    )
    reporting.add_argument(
        "--slack-channel-id",
        metavar="CHANNEL",
        default="",
        help=(
            "Slack channel ID for run notification (requires slack-webhook secret in the namespace). "
            "Omit to suppress Slack."
        ),
    )


def make_parser(description: str = "", epilog: str | None = None) -> CliArgumentParser:
    desc = textwrap.dedent(description or "").strip() or "Konflux OLM pipeline CLI."
    epi = None if epilog is None else textwrap.dedent(epilog).strip()
    prog = Path(sys.argv[0]).name if sys.argv else "olm_pipeline.py"
    parser = CliArgumentParser(
        prog=prog,
        formatter_class=CliHelpFormatter,
        description=desc,
        epilog=epi,
    )
    _add_product_group(parser)
    _add_tests_group(parser)
    _add_external_group(parser)
    _add_konflux_group(parser)
    _add_reporting_group(parser)
    return parser
