#!/usr/bin/env python3
"""
Konflux OLM pipeline CLI — olminstall IntegrationTestScenario (ITS) helper.

Apply or patch the ITS in a tenant namespace, optionally pin or resolve the FBC/catalog container image,
kick off the integration PipelineRun, stream logs (via tkn or polling),
and fall back to KubeArchive when PipelineRuns are pruned from the live cluster.

With no command-line arguments, prints this help message (same as ``-h`` / ``--help``).

External tools (not installed via pip):
  oc     Required — Konflux / OpenShift CLI (login required).
  tkn    Recommended — live PipelineRun logs; otherwise this script polls with oc.
  yq     Required only for --konflux-repo / --konflux-branch / --channel / --product odh overrides.
  skopeo Optional — --product odh when Konflux snapshots are unavailable.

Environment variables:
  KONFLUX_UI                  Optional — Konflux UI base URL for printed links.
  KA_HOST                     KubeArchive API base URL for archived runs/logs.
  PR_APPEAR_TIMEOUT_SECONDS   Seconds to wait for PipelineRun after snapshot (default: 600).

Examples:
  %(prog)s --watch
  %(prog)s --watch odh-olminstall-smoke-testops-abcde
  %(prog)s --list-pipelines
  %(prog)s --list-pipelines 20
  %(prog)s --product rhoai
  %(prog)s --product rhoai --version 3.5
  %(prog)s --product odh
  %(prog)s --image quay.io/rhoai/rhoai-fbc-fragment@sha256:...
  %(prog)s --konflux-repo https://github.com/you/fork.git --konflux-branch my-feature

Exit codes:
  0   Success
  1   General error
  2   Invalid arguments
  130 Interrupted (Ctrl-C)

Python dependencies: none beyond the standard library (see requirements.txt in this directory).
"""

from __future__ import annotations

import atexit
import sys
from pathlib import Path

_OLMINSTALL_DIR = Path(__file__).resolve().parent
if str(_OLMINSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_OLMINSTALL_DIR))

from helpers.cli import emit_click_style_error, make_parser, parse_cli_args
from helpers.errors import AppError
from helpers.runner import OLMInstallRunner


def main(argv: list[str] | None = None) -> int:
    argv_list = argv if argv is not None else sys.argv[1:]
    parser = make_parser(__doc__)
    if not argv_list:
        parser.print_help()
        return 0
    try:
        args = parse_cli_args(parser, argv_list)
        runner = OLMInstallRunner(args)
        atexit.register(runner.cleanup)
        return runner.run()
    except KeyboardInterrupt:
        return 130
    except AppError as exc:
        emit_click_style_error(parser, str(exc), usage=(exc.code == 2))
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
