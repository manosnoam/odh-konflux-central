"""Detect RHOAI catalog line (2.25, 3.5-ea.2, …) from Konflux Snapshot PAC metadata."""

from __future__ import annotations

import re

from suite.component_version_gate import normalize_version_for_enablement, rhoai_version_at_least

_PRNAME_VERSION_RE = re.compile(
    r"rhoai-fbc-fragment-rhoai-(\d+)(?:-ea(\d+))?-ocp-",
    re.IGNORECASE,
)
_RHOAI_LINE_RE = re.compile(
    r"rhoai-(\d+(?:\.\d+)*(?:-ea\.\d+)?)",
    re.IGNORECASE,
)
_CEL_CATALOG_PATH_RE = re.compile(
    r"catalog/rhoai-([^/\"']+)",
    re.IGNORECASE,
)

_PAC_PRNAME = "pac.test.appstudio.openshift.io/original-prname"
_PAC_TITLE = "pac.test.appstudio.openshift.io/sha-title"
_PAC_CEL = "pac.test.appstudio.openshift.io/on-cel-expression"
_RESULT_IMAGE = "test.appstudio.openshift.io/result-image-url"


def _compact_digits_to_version(digits: str) -> str:
    """``225`` → ``2.25``, ``33`` → ``3.3``, ``35`` → ``3.5``."""
    d = (digits or "").strip()
    if not d.isdigit():
        return d
    if len(d) == 3:
        return f"{d[0]}.{d[1:]}"
    if len(d) == 2:
        return f"{d[0]}.{d[1]}"
    if len(d) == 1:
        return d
    if len(d) > 3 and d[1:].isdigit():
        return f"{d[0]}.{d[1:]}"
    return d


def catalog_line_from_prname(prname: str) -> str:
    match = _PRNAME_VERSION_RE.search((prname or "").strip())
    if not match:
        return ""
    base = _compact_digits_to_version(match.group(1))
    ea = match.group(2)
    if ea is not None:
        return f"{base}-ea.{ea}"
    return base


def _catalog_line_from_text(text: str) -> str:
    match = _RHOAI_LINE_RE.search((text or "").strip())
    if not match:
        return ""
    return match.group(1).strip()


def catalog_line_from_free_text(text: str) -> str:
    return _catalog_line_from_text(text)


def catalog_line_from_image_tag(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    return _catalog_line_from_text(text.rsplit(":", 1)[-1])


def catalog_line_from_cel_expression(cel: str) -> str:
    match = _CEL_CATALOG_PATH_RE.search((cel or "").strip())
    if not match:
        return ""
    raw = match.group(1).strip()
    return catalog_line_from_free_text(f"rhoai-{raw}")


def catalog_line_from_snapshot_metadata(
    labels: dict[str, str] | None,
    annotations: dict[str, str] | None,
) -> str:
    """Best-effort catalog line from Integration Service Snapshot labels/annotations."""
    lab = labels if isinstance(labels, dict) else {}
    ann = annotations if isinstance(annotations, dict) else {}
    sources = (
        lab.get(_PAC_PRNAME, ""),
        ann.get(_PAC_TITLE, ""),
        ann.get(_RESULT_IMAGE, ""),
        ann.get(_PAC_CEL, ""),
    )
    extractors = (
        catalog_line_from_prname,
        catalog_line_from_free_text,
        catalog_line_from_image_tag,
        catalog_line_from_cel_expression,
    )
    for extractor, value in zip(extractors, sources):
        if line := extractor(value):
            return line
    return ""


def catalog_line_meets_min_version(catalog_line: str, min_version: str) -> bool:
    line = (catalog_line or "").strip()
    minimum = (min_version or "3.5").strip() or "3.5"
    if not line:
        return True
    compare_line, is_numeric = normalize_version_for_enablement(line)
    if not is_numeric:
        return True
    return rhoai_version_at_least(compare_line, minimum)
