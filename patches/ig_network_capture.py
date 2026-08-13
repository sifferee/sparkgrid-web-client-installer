#!/usr/bin/env python3
"""Focused Instagram GraphQL/private API capture for local diagnostics.

The capture is intentionally split into two trees:

- network/raw: complete local material, including sensitive HTTP headers.
  Never upload or commit this folder. Files are chmod 0600 where supported.
- network/export: redacted HAR/JSONL that is safer to share for debugging.

Only Instagram XHR/fetch/private API traffic is retained. Static images, video
segments, fonts and unrelated third-party analytics are excluded.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_CAPTURE_HOST_SUFFIXES = (
    "instagram.com",
    "cdninstagram.com",
)
_CAPTURE_EXACT_HOSTS = {
    "rupload.facebook.com",
    "graph.instagram.com",
}
_PRIVATE_PATH_MARKERS = (
    "/graphql",
    "/api/v1/",
    "/ajax/",
    "/rupload",
    "rupload_ig",
    "/media/",
    "/clips/",
    "/reels/",
    "/web/",
)
_TEXT_CONTENT_MARKERS = (
    "application/json",
    "application/graphql",
    "application/x-www-form-urlencoded",
    "text/",
    "javascript",
    "xml",
)
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "proxy-authenticate",
    "cookie",
    "set-cookie",
    "x-csrftoken",
    "x-ig-www-claim",
    "x-mid",
    "x-fb-lsd",
    "x-asbd-id",
}
_SENSITIVE_KEY_RE = re.compile(
    r"(?:pass(?:word)?|passwd|secret|token|session|cookie|csrf|authorization|claim|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|device[_-]?id|machine[_-]?id)",
    re.I,
)
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "token",
    "csrf_token",
    "sessionid",
    "password",
    "authorization",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"binary_bytes": len(value), "sha256": hashlib.sha256(bytes(value)).hexdigest()}
    return str(value)


def _write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def _append_jsonl(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def _safe_file_piece(value: str, limit: int = 70) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:limit] or "item"


def _is_interesting_url(url: str, resource_type: str = "") -> bool:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return False
    if not host:
        return False
    host_ok = host in _CAPTURE_EXACT_HOSTS or any(host == suffix or host.endswith("." + suffix) for suffix in _CAPTURE_HOST_SUFFIXES)
    if not host_ok:
        return False
    if host == "rupload.facebook.com":
        return True
    if resource_type in {"xhr", "fetch", "eventsource", "websocket"}:
        return True
    return any(marker in path for marker in _PRIVATE_PATH_MARKERS)


def _redact_scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted len={len(text)} sha256={digest}>"


def _redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in dict(headers or {}).items():
        if str(key).lower() in _SENSITIVE_HEADERS:
            out[str(key)] = _redact_scalar(value)
        else:
            out[str(key)] = value
    return out


def _redact_payload(value: Any, parent_key: str = "") -> Any:
    if parent_key and _SENSITIVE_KEY_RE.search(parent_key):
        return _redact_scalar(value)
    if isinstance(value, dict):
        return {str(k): _redact_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, parent_key) for item in value]
    if isinstance(value, str):
        # Redact common form/query fragments while preserving the rest of the
        # payload for endpoint reconstruction.
        text = value
        text = re.sub(
            r'(?i)(password|passwd|csrf(?:token)?|sessionid|access_token|authorization|cookie|x-ig-www-claim)=([^&\s]+)',
            lambda m: m.group(1) + '=' + _redact_scalar(m.group(2)),
            text,
        )
        return text
    return value


def _redact_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in _SENSITIVE_QUERY_KEYS or _SENSITIVE_KEY_RE.search(key):
                value = _redact_scalar(value)
            pairs.append((key, value))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(pairs, doseq=True), parsed.fragment))
    except Exception:
        return url


def _headers_to_har(headers: Dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": str(k), "value": "" if v is None else str(v)} for k, v in dict(headers or {}).items()]


def _query_to_har(url: str) -> list[dict[str, str]]:
    try:
        return [{"name": k, "value": v} for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    except Exception:
        return []


def _content_type(headers: Dict[str, Any]) -> str:
    for key, value in dict(headers or {}).items():
        if str(key).lower() == "content-type":
            return str(value or "")
    return ""


def _body_is_textual(content_type: str) -> bool:
    low = str(content_type or "").lower()
    return any(marker in low for marker in _TEXT_CONTENT_MARKERS)


def _decode_body(data: bytes, content_type: str) -> tuple[Optional[str], Optional[Any], str]:
    if not data:
        return "", None, "text"
    if not _body_is_textual(content_type):
        return None, None, "binary"
    text = data.decode("utf-8", errors="replace")
    parsed: Optional[Any] = None
    if "json" in str(content_type or "").lower() or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
    return text, parsed, "json" if parsed is not None else "text"


class InstagramNetworkCapture:
    """Capture selected Instagram network transactions from a BrowserContext."""

    def __init__(
        self,
        context: Any,
        root: Path | str,
        *,
        account: str = "",
        run_id: str = "",
        phase: str = "upload",
        max_request_body_bytes: Optional[int] = None,
        max_response_body_bytes: Optional[int] = None,
    ) -> None:
        self.context = context
        self.root = Path(root) / "network"
        self.account = str(account or "")
        self.run_id = str(run_id or "")
        self.phase = str(phase or "capture")
        # Full headers can contain cookies/CSRF/session material. Keep the raw
        # capture outside the web-served debug tree and place only redacted
        # exports next to screenshots/actions.
        data_root = Path(os.environ.get("SPARKGRID_DATA_DIR") or Path(root).parents[3])
        self.raw = (
            data_root
            / "network_capture_private"
            / _safe_file_piece(self.run_id or "run")
            / _safe_file_piece(self.account or "account")
            / _safe_file_piece(self.phase or "capture")
        )
        self.export = self.root / "export"
        self.raw_bodies = self.raw / "bodies"
        self.export_bodies = self.export / "bodies"
        request_mb = float(os.environ.get("SPARKGRID_NETWORK_MAX_REQUEST_MB", "2") or 2)
        response_mb = float(os.environ.get("SPARKGRID_NETWORK_MAX_RESPONSE_MB", "12") or 12)
        self.max_request_body_bytes = int(max_request_body_bytes or request_mb * 1024 * 1024)
        self.max_response_body_bytes = int(max_response_body_bytes or response_mb * 1024 * 1024)
        self._lock = threading.RLock()
        self._seq = 0
        self._request_ids: Dict[int, str] = {}
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._har_entries_raw: list[Dict[str, Any]] = []
        self._har_entries_export: list[Dict[str, Any]] = []
        self._started = False
        self._stopped = False
        self.stats = {
            "requests": 0,
            "responses": 0,
            "finished": 0,
            "failed": 0,
            "json_responses": 0,
            "request_body_bytes_saved": 0,
            "response_body_bytes_saved": 0,
            "response_bodies_skipped_large": 0,
        }

    def _next_id(self, request: Any) -> str:
        key = id(request)
        with self._lock:
            existing = self._request_ids.get(key)
            if existing:
                return existing
            self._seq += 1
            req_id = f"req_{self._seq:05d}"
            self._request_ids[key] = req_id
            return req_id

    def _request_body(self, request: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"kind": "none", "size": 0}
        try:
            parsed = request.post_data_json
            if parsed is not None:
                encoded = json.dumps(parsed, ensure_ascii=False, default=_json_default).encode("utf-8")
                payload.update({"kind": "json", "size": len(encoded), "value": parsed})
                return payload
        except Exception:
            pass
        try:
            text = request.post_data
        except Exception:
            text = None
        if text is not None:
            encoded = str(text).encode("utf-8", errors="replace")
            payload.update({"kind": "text", "size": len(encoded)})
            if len(encoded) <= self.max_request_body_bytes:
                payload["value"] = str(text)
            else:
                payload["omitted"] = "request body exceeds capture limit"
                payload["sha256"] = hashlib.sha256(encoded).hexdigest()
            return payload
        try:
            buf = request.post_data_buffer
        except Exception:
            buf = None
        if buf:
            data = bytes(buf)
            payload.update({
                "kind": "binary",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "omitted": "binary upload body is not copied",
            })
        return payload

    def start(self) -> "InstagramNetworkCapture":
        if self._started:
            return self
        self.raw_bodies.mkdir(parents=True, exist_ok=True)
        self.export_bodies.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.raw, 0o700)
        except Exception:
            pass
        warning = (
            "SENSITIVE LOCAL CAPTURE\n\n"
            f"Full raw capture: {self.raw}\n"
            "It may contain Cookie, CSRF, session and device headers.\n"
            "Do not upload, commit or send the private raw folder.\n"
            "Use network/export for normal diagnostics; it redacts common secrets.\n"
            "Binary video upload bodies are never copied.\n"
        )
        (self.root / "README_SENSITIVE.txt").write_text(warning, encoding="utf-8")
        (self.raw / "README_DO_NOT_SHARE.txt").write_text(warning, encoding="utf-8")
        try:
            os.chmod(self.root / "README_SENSITIVE.txt", 0o600)
            os.chmod(self.raw / "README_DO_NOT_SHARE.txt", 0o600)
        except Exception:
            pass
        self.context.on("request", self._on_request)
        self.context.on("response", self._on_response)
        self.context.on("requestfinished", self._on_request_finished)
        self.context.on("requestfailed", self._on_request_failed)
        self._started = True
        self._write_manifest(active=True)
        return self

    def _on_request(self, request: Any) -> None:
        try:
            url = str(request.url or "")
            resource_type = str(request.resource_type or "")
            if not _is_interesting_url(url, resource_type):
                return
            req_id = self._next_id(request)
            try:
                headers = dict(request.all_headers() or {})
            except Exception:
                headers = dict(getattr(request, "headers", {}) or {})
            body = self._request_body(request)
            body_value = body.get("value") if isinstance(body, dict) else None
            operation_name = ""
            doc_id = ""
            if isinstance(body_value, dict):
                operation_name = str(
                    body_value.get("fb_api_req_friendly_name")
                    or body_value.get("operationName")
                    or body_value.get("query_name")
                    or ""
                )
                doc_id = str(body_value.get("doc_id") or body_value.get("docId") or "")
            if not operation_name:
                operation_name = str(headers.get("x-fb-friendly-name") or headers.get("x-fb-request-name") or "")
            payload = {
                "id": req_id,
                "run_id": self.run_id,
                "account": self.account,
                "phase": self.phase,
                "ts": _utc_iso(),
                "method": str(request.method or "GET"),
                "url": url,
                "resource_type": resource_type,
                "is_navigation_request": bool(request.is_navigation_request()),
                "headers": headers,
                "payload": body,
                "operation_name": operation_name,
                "doc_id": doc_id,
            }
            redacted = dict(payload)
            redacted["url"] = _redact_url(url)
            redacted["headers"] = _redact_headers(headers)
            redacted["payload"] = _redact_payload(body)
            with self._lock:
                self.stats["requests"] += 1
                self._entries[req_id] = {
                    "request": payload,
                    "request_redacted": redacted,
                    "started_epoch": time.time(),
                    "response": None,
                    "response_redacted": None,
                }
            _append_jsonl(self.raw / "requests.jsonl", payload)
            _append_jsonl(self.export / "requests.redacted.jsonl", redacted)
            if body.get("value") is not None:
                suffix = ".json" if body.get("kind") == "json" else ".txt"
                target = self.raw_bodies / f"{req_id}_request{suffix}"
                if suffix == ".json":
                    _write_json(target, body.get("value"))
                else:
                    target.write_text(str(body.get("value") or ""), encoding="utf-8")
                    try:
                        os.chmod(target, 0o600)
                    except Exception:
                        pass
                redacted_value = _redact_payload(body.get("value"))
                red_target = self.export_bodies / f"{req_id}_request{suffix}"
                if suffix == ".json":
                    _write_json(red_target, redacted_value)
                else:
                    red_target.write_text(str(redacted_value or ""), encoding="utf-8")
                self.stats["request_body_bytes_saved"] += int(body.get("size") or 0)
        except Exception:
            return

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            req_id = self._request_ids.get(id(request))
            if not req_id:
                return
            try:
                headers = dict(response.all_headers() or {})
            except Exception:
                headers = dict(getattr(response, "headers", {}) or {})
            payload = {
                "id": req_id,
                "run_id": self.run_id,
                "account": self.account,
                "phase": self.phase,
                "ts": _utc_iso(),
                "url": str(response.url or ""),
                "status": int(response.status or 0),
                "status_text": str(response.status_text or ""),
                "ok": bool(response.ok),
                "headers": headers,
                "content_type": _content_type(headers),
                "from_service_worker": bool(getattr(response, "from_service_worker", False)),
            }
            redacted = dict(payload)
            redacted["url"] = _redact_url(payload["url"])
            redacted["headers"] = _redact_headers(headers)
            with self._lock:
                self.stats["responses"] += 1
                if req_id in self._entries:
                    self._entries[req_id]["response"] = payload
                    self._entries[req_id]["response_redacted"] = redacted
            _append_jsonl(self.raw / "responses.jsonl", payload)
            _append_jsonl(self.export / "responses.redacted.jsonl", redacted)
        except Exception:
            return

    def _on_request_finished(self, request: Any) -> None:
        req_id = self._request_ids.get(id(request))
        if not req_id:
            return
        try:
            response = request.response()
        except Exception:
            response = None
        body_meta: Dict[str, Any] = {"kind": "none", "size": 0}
        if response is not None:
            try:
                headers = dict(response.all_headers() or {})
            except Exception:
                headers = dict(getattr(response, "headers", {}) or {})
            content_type = _content_type(headers)
            try:
                data = bytes(response.body() or b"")
            except Exception as exc:
                data = b""
                body_meta["error"] = type(exc).__name__
            body_meta["size"] = len(data)
            if data:
                body_meta["sha256"] = hashlib.sha256(data).hexdigest()
                if len(data) > self.max_response_body_bytes:
                    body_meta.update({"kind": "omitted", "omitted": "response body exceeds capture limit"})
                    self.stats["response_bodies_skipped_large"] += 1
                else:
                    text, parsed, kind = _decode_body(data, content_type)
                    body_meta["kind"] = kind
                    if kind == "json" and parsed is not None:
                        body_meta["value"] = parsed
                        self.stats["json_responses"] += 1
                        _write_json(self.raw_bodies / f"{req_id}_response.json", parsed)
                        _write_json(self.export_bodies / f"{req_id}_response.json", _redact_payload(parsed))
                    elif kind == "text" and text is not None:
                        body_meta["value"] = text
                        target = self.raw_bodies / f"{req_id}_response.txt"
                        target.write_text(text, encoding="utf-8")
                        try:
                            os.chmod(target, 0o600)
                        except Exception:
                            pass
                        red_target = self.export_bodies / f"{req_id}_response.txt"
                        red_target.write_text(str(_redact_payload(text)), encoding="utf-8")
                    else:
                        body_meta["sample_base64"] = base64.b64encode(data[:96]).decode("ascii")
                        body_meta["omitted"] = "binary response body is not copied"
                    self.stats["response_body_bytes_saved"] += len(data) if kind in {"json", "text"} else 0
        with self._lock:
            entry = self._entries.get(req_id)
            if not entry:
                return
            entry["body"] = body_meta
            entry["finished_epoch"] = time.time()
            self.stats["finished"] += 1
        self._finalize_entry(req_id, failed=None)

    def _on_request_failed(self, request: Any) -> None:
        req_id = self._request_ids.get(id(request))
        if not req_id:
            return
        try:
            failure = request.failure
        except Exception:
            failure = "request failed"
        with self._lock:
            self.stats["failed"] += 1
        self._finalize_entry(req_id, failed=str(failure or "request failed"))

    def _finalize_entry(self, req_id: str, failed: Optional[str]) -> None:
        with self._lock:
            item = self._entries.get(req_id)
            if not item or item.get("finalized"):
                return
            item["finalized"] = True
            request_payload = item.get("request") or {}
            request_redacted = item.get("request_redacted") or {}
            response_payload = item.get("response") or {
                "status": 0,
                "status_text": failed or "",
                "headers": {},
                "content_type": "",
                "url": request_payload.get("url", ""),
            }
            response_redacted = item.get("response_redacted") or {
                **response_payload,
                "headers": _redact_headers(response_payload.get("headers") or {}),
                "url": _redact_url(response_payload.get("url") or ""),
            }
            body = item.get("body") or {"kind": "none", "size": 0}
            elapsed_ms = max(0.0, (float(item.get("finished_epoch") or time.time()) - float(item.get("started_epoch") or time.time())) * 1000.0)

            transaction = {
                "id": req_id,
                "run_id": self.run_id,
                "account": self.account,
                "phase": self.phase,
                "startedDateTime": request_payload.get("ts") or _utc_iso(),
                "time_ms": round(elapsed_ms, 2),
                "request": request_payload,
                "response": response_payload,
                "response_body": body,
                "failure": failed or "",
            }
            transaction_redacted = {
                **transaction,
                "request": request_redacted,
                "response": response_redacted,
                "response_body": _redact_payload(body),
            }
            _append_jsonl(self.raw / "transactions.jsonl", transaction)
            _append_jsonl(self.export / "transactions.redacted.jsonl", transaction_redacted)
            self._har_entries_raw.append(self._to_har_entry(transaction, redacted=False))
            self._har_entries_export.append(self._to_har_entry(transaction_redacted, redacted=True))

    def _to_har_entry(self, transaction: Dict[str, Any], *, redacted: bool) -> Dict[str, Any]:
        req = transaction.get("request") or {}
        resp = transaction.get("response") or {}
        req_body = req.get("payload") or {}
        resp_body = transaction.get("response_body") or {}
        request_post_data = None
        if req_body.get("value") is not None:
            value = req_body.get("value")
            text = json.dumps(value, ensure_ascii=False, default=_json_default) if isinstance(value, (dict, list)) else str(value)
            request_post_data = {
                "mimeType": _content_type(req.get("headers") or {}),
                "text": text,
            }
        content: Dict[str, Any] = {
            "size": int(resp_body.get("size") or 0),
            "mimeType": str(resp.get("content_type") or ""),
        }
        if resp_body.get("value") is not None:
            value = resp_body.get("value")
            content["text"] = json.dumps(value, ensure_ascii=False, default=_json_default) if isinstance(value, (dict, list)) else str(value)
        entry = {
            "startedDateTime": transaction.get("startedDateTime") or _utc_iso(),
            "time": float(transaction.get("time_ms") or 0.0),
            "request": {
                "method": str(req.get("method") or "GET"),
                "url": str(req.get("url") or ""),
                "httpVersion": "",
                "cookies": [],
                "headers": _headers_to_har(req.get("headers") or {}),
                "queryString": _query_to_har(req.get("url") or ""),
                "headersSize": -1,
                "bodySize": int(req_body.get("size") or 0),
            },
            "response": {
                "status": int(resp.get("status") or 0),
                "statusText": str(resp.get("status_text") or transaction.get("failure") or ""),
                "httpVersion": "",
                "cookies": [],
                "headers": _headers_to_har(resp.get("headers") or {}),
                "content": content,
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": int(resp_body.get("size") or 0),
            },
            "cache": {},
            "timings": {"send": 0, "wait": float(transaction.get("time_ms") or 0.0), "receive": 0},
            "_sparkgrid": {
                "id": transaction.get("id"),
                "phase": self.phase,
                "resourceType": req.get("resource_type"),
                "failure": transaction.get("failure") or "",
                "redacted": bool(redacted),
            },
        }
        if request_post_data is not None:
            entry["request"]["postData"] = request_post_data
        return entry

    def _write_hars(self) -> None:
        common = {
            "version": "1.2",
            "creator": {"name": "SparkGrid Instagram Network Capture", "version": "3.0"},
            "pages": [],
        }
        raw_har = {"log": {**common, "entries": list(self._har_entries_raw)}}
        export_har = {"log": {**common, "entries": list(self._har_entries_export)}}
        _write_json(self.raw / "instagram_private.har", raw_har)
        _write_json(self.export / "instagram_private.redacted.har", export_har)

    def _write_endpoint_index(self) -> None:
        grouped: Dict[str, Dict[str, Any]] = {}
        for entry in self._har_entries_export:
            req = entry.get("request") or {}
            resp = entry.get("response") or {}
            meta = entry.get("_sparkgrid") or {}
            try:
                parsed = urlparse(str(req.get("url") or ""))
                host = parsed.hostname or ""
                path = parsed.path or "/"
                query_keys = sorted({k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)})
            except Exception:
                host, path, query_keys = "", "", []
            key = f"{req.get('method','GET')} {host}{path}"
            item = grouped.setdefault(key, {
                "method": req.get("method") or "GET",
                "host": host,
                "path": path,
                "count": 0,
                "statuses": {},
                "resource_types": {},
                "query_keys": set(),
                "sample_request_ids": [],
            })
            item["count"] += 1
            status_key = str(resp.get("status") or 0)
            item["statuses"][status_key] = int(item["statuses"].get(status_key) or 0) + 1
            resource = str(meta.get("resourceType") or "unknown")
            item["resource_types"][resource] = int(item["resource_types"].get(resource) or 0) + 1
            item["query_keys"].update(query_keys)
            if len(item["sample_request_ids"]) < 5:
                item["sample_request_ids"].append(meta.get("id"))
        endpoints = []
        for item in grouped.values():
            item["query_keys"] = sorted(item["query_keys"])
            endpoints.append(item)
        endpoints.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("host") or ""), str(x.get("path") or "")))
        _write_json(self.export / "endpoint_index.json", {
            "run_id": self.run_id,
            "account": self.account,
            "phase": self.phase,
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
        })

    def _write_manifest(self, *, active: bool) -> None:
        payload = {
            "schema_version": 1,
            "active": bool(active),
            "run_id": self.run_id,
            "account": self.account,
            "phase": self.phase,
            "created_at": _utc_iso(),
            "capture_filter": "Instagram xhr/fetch/private API + rupload.facebook.com",
            "raw_path": str(self.raw),
            "redacted_export_path": str(self.export),
            "raw_is_sensitive": True,
            "binary_upload_bodies_copied": False,
            "limits": {
                "request_body_bytes": self.max_request_body_bytes,
                "response_body_bytes": self.max_response_body_bytes,
            },
            "stats": dict(self.stats),
        }
        _write_json(self.root / "capture_manifest.json", payload)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Finalize requests whose requestfinished event was not delivered yet.
        with self._lock:
            pending = [key for key, value in self._entries.items() if not value.get("finalized")]
        for req_id in pending:
            self._finalize_entry(req_id, failed="capture stopped before requestfinished")
        self._write_hars()
        self._write_endpoint_index()
        self._write_manifest(active=False)
        for event, callback in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfinished", self._on_request_finished),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                self.context.remove_listener(event, callback)
            except Exception:
                try:
                    self.context.off(event, callback)
                except Exception:
                    pass


def start_instagram_network_capture(
    context: Any,
    root: Path | str,
    *,
    account: str = "",
    run_id: str = "",
    phase: str = "upload",
) -> Optional[InstagramNetworkCapture]:
    enabled = str(os.environ.get("SPARKGRID_NETWORK_CAPTURE", "0") or "0").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    try:
        return InstagramNetworkCapture(
            context,
            root,
            account=account,
            run_id=run_id,
            phase=phase,
        ).start()
    except Exception:
        return None


__all__ = [
    "InstagramNetworkCapture",
    "start_instagram_network_capture",
    "_is_interesting_url",
]
