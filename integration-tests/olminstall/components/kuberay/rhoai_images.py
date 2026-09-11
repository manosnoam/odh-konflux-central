"""KubeRay RHOAI image test patches for EPHC IDMS registry.redhat.io mirror parity."""

from __future__ import annotations

_IMAGES_TEST = "test/e2e/raycluster_rhoai_images_test.go"
_IDMS_MARK = "olminstall-kuberay-idms"
_KUBE_RBAC_MSG = (
    "injected kube-rbac-proxy sidecar should use RELATED_IMAGE_ODH_KUBE_RBAC_PROXY_IMAGE"
)


def _rhoai_idms_patch_python_body() -> str:
    return "\n".join(
        [
            "from pathlib import Path",
            f"mark = {_IDMS_MARK!r}",
            f"needle = {_KUBE_RBAC_MSG!r}",
            f"p = Path({_IMAGES_TEST!r})",
            "text = p.read_text()",
            "if mark in text:",
            "    raise SystemExit(0)",
            'if needle not in text:',
            '    raise SystemExit("kube-rbac-proxy RELATED_IMAGE assertion not found")',
            "lines = text.splitlines(True)",
            "out = []",
            "for line in lines:",
            "    if needle in line:",
            "        indent = line[: len(line) - len(line.lstrip())]",
            '        out.append(',
            '            f\'{indent}sidecarImage = strings.Replace(sidecarImage, "registry.redhat.io/", "quay.io/", 1) // {mark}\\n\'',
            "        )",
            "    out.append(line)",
            'text = "".join(out)',
            'if \'"strings"\' not in text and "strings.Replace" in text:',
            '    text = text.replace("import (\\n", \'import (\\n\\t"strings"\\n\', 1)',
            "p.write_text(text)",
            'print("kuberay: patched TestRayClusterRHOAIImages for EPHC IDMS mirror", flush=True)',
        ]
    ) + "\n"


def kuberay_rhoai_idms_patch_shell() -> str:
    """Normalize registry.redhat.io sidecar images before RELATED_IMAGE comparison on EPHC."""
    return f"if [ -f {_IMAGES_TEST} ]; then python3 - <<'PY'\n{_rhoai_idms_patch_python_body()}PY\nfi"


def prepend_kuberay_smoke_patch(run_command: str) -> str:
    cmd = (run_command or "").strip()
    if not cmd:
        return cmd
    from components.kuberay.auth_options import kuberay_skip_auth_options_if_crd_missing_shell

    patches = " && ".join([
        kuberay_skip_auth_options_if_crd_missing_shell(),
        kuberay_rhoai_idms_patch_shell(),
    ])
    return f"{patches} && {cmd}"
