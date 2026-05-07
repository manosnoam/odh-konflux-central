"""OpenShift `oc` subprocess helpers."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from .errors import AppError


def run_cmd(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        text=True,
        input=input_text,
        capture_output=capture,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AppError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip() or proc.stdout.strip()}",
            1,
        )
    return proc


def parse_json_output(cmd: list[str]) -> dict[str, Any]:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def get_jsonpath(cmd: list[str]) -> str:
    proc = run_cmd(cmd, capture=True, check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def ts_now() -> str:
    return time.strftime("%H:%M:%S")


def filter_warning_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("Warning"))
