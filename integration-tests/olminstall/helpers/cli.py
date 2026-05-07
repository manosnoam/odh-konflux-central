"""Argument parsing and Click-style usage errors for ``run_olm_pipeline.py``."""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import NoReturn

from .constants import (
    DEFAULT_APP,
    DEFAULT_LIST_COUNT,
    DEFAULT_NAMESPACE,
    DEFAULT_PRODUCT,
    PRODUCT_CHOICES,
)
from .errors import AppError


class CliHelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Keep epilog layout; append option defaults when helpful (similar intent to Click ``show_default``)."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_txt = action.help
        if help_txt is None:
            help_txt = ""
        if "%(default)" in help_txt:
            return super()._get_help_string(action)
        optional_value = action.nargs in (None, argparse.OPTIONAL, argparse.ZERO_OR_MORE)
        if (
            action.option_strings
            and optional_value
            and not action.required
            and action.default is not argparse.SUPPRESS
        ):
            if action.default == "":
                return help_txt
            if action.default is None and action.nargs == argparse.OPTIONAL:
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


def make_parser(reference_doc: str | None) -> CliArgumentParser:
    reference = textwrap.dedent(reference_doc or "").strip()
    summary = reference.split("\n", 1)[0].strip()
    prog = Path(sys.argv[0]).name if sys.argv else "run_olm_pipeline.py"
    parser = CliArgumentParser(
        prog=prog,
        formatter_class=CliHelpFormatter,
        description=summary,
        epilog=reference,
    )
    parser.add_argument(
        "--image",
        default="",
        metavar="REF",
        help="FBC/catalog container image (e.g. quay.io/...@sha256:…); omit to resolve automatically",
    )
    parser.add_argument("--app", default=DEFAULT_APP, help="Konflux application label")
    parser.add_argument("--namespace", "-n", default=DEFAULT_NAMESPACE, help="Tenant namespace")
    parser.add_argument("--konflux-repo", metavar="URL", default="", help="SCRIPTS_REPO_URL and ITS resolver URL (needs yq)")
    parser.add_argument("--konflux-branch", metavar="REF", default="", help="SCRIPTS_REPO_REVISION and ITS resolver revision (needs yq)")
    parser.add_argument(
        "--channel",
        metavar="NAME",
        default="",
        help="UPDATE_CHANNEL on the patched ITS (see examples below)",
    )
    parser.add_argument(
        "--product",
        default=DEFAULT_PRODUCT,
        choices=PRODUCT_CHOICES,
        help="Product stream for ITS wiring and catalog/snapshot resolution",
    )
    parser.add_argument(
        "--version",
        "--rhoai-version",
        dest="version",
        metavar="VER",
        default="",
        help="RHOAI only: resolve FBC fragment from rhoai-v<VER>* application labels",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const="",
        default=None,
        metavar="PIPELINERUN",
        help="Watch latest owned olminstall PipelineRun, or the named run (KubeArchive when pruned)",
    )
    parser.add_argument(
        "--list-pipelines",
        nargs="?",
        const=str(DEFAULT_LIST_COUNT),
        default=None,
        metavar="N",
        help=f"List latest N PipelineRuns for --app; omit N to use {DEFAULT_LIST_COUNT}",
    )
    return parser


def parse_cli_args(parser: CliArgumentParser, argv: list[str]) -> argparse.Namespace:
    args = parser.parse_args(argv)

    if args.version and args.product != "rhoai":
        raise AppError("--version is supported only with --product rhoai", 2)

    if args.list_pipelines is not None:
        try:
            lp = int(args.list_pipelines)
            if lp <= 0:
                raise ValueError
            args.list_pipelines = lp
        except ValueError as exc:
            raise AppError(f"--list-pipelines expects a positive integer (got: {args.list_pipelines})", 2) from exc
    else:
        args.list_pipelines = 0

    args.watch_mode = args.watch is not None
    return args
