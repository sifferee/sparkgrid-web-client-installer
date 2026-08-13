"""Secret-free generic classification for mobile rotation provider replies."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_CLASSIFIER_FIELDS = (
    "status", "state", "result", "message", "error", "code", "detail",
    "retry_after", "cooldown",
)
_SENSITIVE_KEY = re.compile(
    r"(?:api[-_]?key|token|secret|password|passwd|credential|authorization|cookie|url)",
    re.I,
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[-_]?key|token|secret|password|passwd|credential|authorization)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_OPAQUE_SECRET = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")


@dataclass(frozen=True)
class RotationResponse:
    state: str
    http_status: int
    response_type: str
    cooldown_seconds: int = 0
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def __iter__(self):
        yield self.ready
        yield self.detail or f"provider_state={self.state}"


def _response_parts(content_type: str, payload: bytes) -> tuple[str, Any, str, str]:
    body = bytes(payload or b"")
    raw = body[:4096].decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed, text = None, raw.lower()[:512]
    else:
        text = " ".join(
            f"{key}={parsed.get(key)}" for key in _CLASSIFIER_FIELDS
            if isinstance(parsed, dict)
            and isinstance(parsed.get(key), (str, int, float, bool))
        ).lower()[:512]
    lowered_type = str(content_type or "").lower()
    looks_html = bool(re.match(r"(?is)^\s*(?:<!doctype\s+html|<html\b)", raw))
    if not body:
        response_format = "empty"
    elif parsed is not None:
        response_format = "json"
    elif "html" in lowered_type or looks_html:
        response_format = "html"
    elif lowered_type.startswith("text/") or (
        "\ufffd" not in raw and all(ch.isprintable() or ch in "\r\n\t" for ch in raw)
    ):
        response_format = "plain_text"
    else:
        response_format = "other"
    response_type = (
        "json" if "json" in lowered_type or isinstance(parsed, dict)
        else "html" if "html" in lowered_type else "text"
    )
    return raw, parsed, text, response_format if response_format else response_type


def _classify(status: int, content_type: str, payload: bytes, retry_after: str = "") -> dict[str, Any]:
    raw, parsed, text, response_format = _response_parts(content_type, payload)
    media_type = str(content_type or "").partition(";")[0].strip().lower()
    response_type = (
        "json" if "json" in str(content_type).lower() or isinstance(parsed, dict)
        else "html" if "html" in str(content_type).lower() else "text"
    )
    cooldown = int(retry_after) if str(retry_after).isdigit() else 0
    if not cooldown:
        match = re.search(r"(?:wait|retry[- ]?after|try again in|cooldown)[^0-9]{0,16}(\d+)\s*(?:s|sec|second)?", text, re.I)
        cooldown = int(match.group(1)) if match else 0
    cooldown = max(0, min(cooldown, 120))
    has = lambda *items: any(item in text for item in items)
    exact_modem_switch_success = (
        status == 200
        and media_type == "application/json"
        and parsed == {"modem_switch": "success"}
    )
    if status == 429: state, rule = "rate_limited", "http_429"
    elif 500 <= status <= 599: state, rule = "transient_error", "http_5xx"
    elif status >= 400:
        if cooldown or has("cooldown", "bad_switch"):
            state, rule = "cooldown", "http_4xx_cooldown"
        elif has("busy", "temporarily unavailable"):
            state, rule = "provider_busy", "http_4xx_busy"
        elif has("invalid", "denied", "reject", "unauthor", "forbidden", "token"):
            state, rule = "rejected", "http_4xx_rejected"
        else:
            state, rule = "permanent_error", "http_4xx_other"
    elif media_type == "application/json" and parsed is None:
        state, rule = "unknown", "no_supported_token"
    elif exact_modem_switch_success:
        # This provider contract acknowledges only that the modem-switch
        # command was accepted.  Exit-IP readiness is a later, independently
        # observed boundary.
        state, rule = "accepted", "json_modem_switch_success"
    elif has("cooldown", "bad_switch"): state, rule = "cooldown", "body_cooldown"
    elif has("busy", "temporarily unavailable"): state, rule = "provider_busy", "body_busy"
    elif has("error", "invalid", "denied", "reject", "failed", "unauthor"): state, rule = "rejected", "body_rejected"
    elif has("in_progress", "in progress", "process switch", "switching", "processing", "changing"): state, rule = "in_progress", "body_in_progress"
    elif has("accepted", "queued", "pending", "requested"): state, rule = "accepted", "body_accepted"
    elif has("ready", "complete", "completed", "rotated", "success", "switched"): state, rule = "ready", "body_ready"
    else: state, rule = "unknown", "no_supported_token"
    recognized_fields = [
        key for key in _CLASSIFIER_FIELDS
        if isinstance(parsed, dict) and key in parsed
    ]
    if exact_modem_switch_success:
        recognized_fields.append("modem_switch")
    token_candidates = (
        "cooldown", "bad_switch", "busy", "temporarily unavailable", "error",
        "invalid", "denied", "reject", "failed", "unauthor", "in_progress",
        "in progress", "process switch", "switching", "processing", "changing",
        "accepted", "queued", "pending", "requested", "ready", "complete",
        "completed", "rotated", "success", "switched",
    )
    recognized_tokens = [token for token in token_candidates if token in text]
    if exact_modem_switch_success:
        recognized_tokens.append("success")
    return {
        "state": state,
        "http_status": int(status or 0),
        "response_type": response_type,
        "cooldown_seconds": cooldown,
        "response_format": response_format,
        "recognized_fields": recognized_fields,
        "recognized_tokens": recognized_tokens,
        "classifier_rule": rule,
        "raw": raw,
        "parsed": parsed,
    }


def _sanitize_text(value: str) -> str:
    text = _URL.sub("<url>", str(value or ""))
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _OPAQUE_SECRET.sub("<redacted>", text)


def _sanitize_json(value: Any, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(str(key or "")):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize_json(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value[:20]]
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value))


def describe_rotation_response(
    status: int, content_type: str, payload: bytes, retry_after: str = "",
) -> dict[str, Any]:
    """Return a credential-safe explanation of a provider response."""
    result = _classify(status, content_type, payload, retry_after)
    parsed = result.pop("parsed")
    raw = result.pop("raw")
    if result["response_format"] == "empty":
        normalized = "<empty>"
    elif parsed is not None:
        normalized = json.dumps(
            _sanitize_json(parsed), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )[:2048]
    else:
        normalized = _sanitize_text(raw)[:2048]
    result["normalized_response"] = normalized
    return result


def classify_rotation_response(status: int, content_type: str, payload: bytes, retry_after: str = "") -> dict[str, Any]:
    result = _classify(status, content_type, payload, retry_after)
    return {
        key: result[key] for key in (
            "state", "http_status", "response_type", "cooldown_seconds",
        )
    }
