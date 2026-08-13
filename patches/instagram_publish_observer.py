"""Reactive, secret-free observation of one Instagram Reel Share action."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import urlparse


_PUBLISH_PATHS = (
    "/api/v1/media/configure_to_clips/",
    "/api/v1/media/configure/",
    "/api/v1/clips/create/",
)
_PERMALINK_RE = re.compile(r"/(?:reel|reels|p)/([A-Za-z0-9_-]+)/?")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def safe_publish_path(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        return ""
    return path if any(marker in path.lower() for marker in _PUBLISH_PATHS) else ""


def _walk_identity(value: Any, found: Dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in {"pk", "media_id"} and not found["media_id"] and isinstance(item, (str, int)):
                found["media_id"] = str(item)
            elif low in {"code", "shortcode"} and not found["shortcode"] and isinstance(item, str):
                found["shortcode"] = item
            elif low in {"permalink", "link", "url"} and not found["permalink"] and isinstance(item, str):
                match = _PERMALINK_RE.search(item)
                if match:
                    found["permalink"] = item
                    found["shortcode"] = found["shortcode"] or match.group(1)
            _walk_identity(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_identity(item, found)


def extract_publish_identity(payload: Any) -> Dict[str, str]:
    found = {"media_id": "", "shortcode": "", "permalink": ""}
    _walk_identity(payload, found)
    if found["shortcode"] and not found["permalink"]:
        found["permalink"] = f"https://www.instagram.com/reel/{found['shortcode']}/"
    return found


class PublishObserver:
    """Observe only known publish endpoints; never retain bodies or headers."""

    def __init__(self, page: Any) -> None:
        self._lock = threading.RLock()
        self._requests: Dict[int, str] = {}
        self._state: Dict[str, Any] = {
            "request_state": "",
            "safe_path": "",
            "http_status": 0,
            "request_started_at": "",
            "request_finished_at": "",
            "media_id": "",
            "shortcode": "",
            "permalink": "",
        }
        self._context = getattr(page, "context", None)
        self._attached = False
        if self._context is not None and hasattr(self._context, "on"):
            self._context.on("request", self._on_request)
            self._context.on("response", self._on_response)
            self._context.on("requestfinished", self._on_finished)
            self._context.on("requestfailed", self._on_failed)
            self._attached = True

    def _path(self, request: Any) -> str:
        return safe_publish_path(str(getattr(request, "url", "") or ""))

    def _on_request(self, request: Any) -> None:
        path = self._path(request)
        if not path:
            return
        with self._lock:
            self._requests[id(request)] = path
            self._state.update(
                request_state="pending", safe_path=path,
                request_started_at=self._state["request_started_at"] or utc_now(),
            )

    def _on_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        path = self._path(request)
        if not path:
            return
        status = int(getattr(response, "status", 0) or 0)
        identity: Dict[str, str] = {}
        try:
            identity = extract_publish_identity(response.json())
        except Exception:
            identity = {}
        with self._lock:
            self._state["safe_path"] = path
            self._state["http_status"] = status
            self._state["request_state"] = (
                "accepted" if 200 <= status < 300 else "rejected" if status >= 400 else "pending"
            )
            for key in ("media_id", "shortcode", "permalink"):
                if identity.get(key):
                    self._state[key] = identity[key]

    def _on_finished(self, request: Any) -> None:
        path = self._path(request)
        if not path:
            return
        with self._lock:
            if self._state["request_state"] == "pending":
                self._state["request_state"] = "finished"
            self._state["request_finished_at"] = utc_now()

    def _on_failed(self, request: Any) -> None:
        path = self._path(request)
        if not path:
            return
        with self._lock:
            self._state["request_state"] = "failed"
            self._state["request_finished_at"] = utc_now()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def close(self) -> None:
        if not self._attached or self._context is None or not hasattr(self._context, "remove_listener"):
            return
        for event, handler in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfinished", self._on_finished),
            ("requestfailed", self._on_failed),
        ):
            try:
                self._context.remove_listener(event, handler)
            except Exception:
                pass
        self._attached = False
