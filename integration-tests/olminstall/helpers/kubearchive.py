"""KubeArchive REST client for archived Tekton resources."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


class KubeArchiveClient:
    def __init__(self, host: str, token: str) -> None:
        self.host = host.rstrip("/")
        self.token = token
        self.available: bool | None = None

    def _request(self, path: str) -> str:
        req = Request(
            f"{self.host}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except URLError:
            return ""

    def check(self) -> bool:
        if self.available is None:
            raw = self._request("/livez")
            try:
                self.available = bool(raw and json.loads(raw).get("code") == 200)
            except json.JSONDecodeError:
                self.available = False
        return bool(self.available)

    def get_json(self, path: str) -> dict[str, Any]:
        raw = self._request(path)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def get_text(self, path: str) -> str:
        return self._request(path)
