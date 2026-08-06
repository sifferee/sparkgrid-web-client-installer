"""Small, allowlisted proxy reliability telemetry.

This intentionally writes only a one-line event to the normal diagnostic
stream.  It never accepts an endpoint, account name, exit IP, request URL, or
provider payload as a field.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


_FIELDS = (
    "proxy_reference_hash", "proxy_type", "phase", "normalized_result",
    "provider_state", "http_status", "cooldown_seconds", "elapsed_ms",
    "connectivity", "ip_changed", "instagram_reachable", "browser_launched",
    "retry_attempt", "final_classification",
)


def proxy_reference_hash(proxy: str) -> str:
    value = str(proxy or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else "direct"


def emit_proxy_telemetry(proxy: str, **values: Any) -> dict[str, Any]:
    """Emit a sanitized, schema-stable proxy event and return it for tests."""
    event = {field: values.get(field, "unknown") for field in _FIELDS}
    event["proxy_reference_hash"] = proxy_reference_hash(proxy)
    event["proxy_type"] = str(event["proxy_type"] or "unknown")
    event["phase"] = str(event["phase"] or "unknown")
    event["normalized_result"] = str(event["normalized_result"] or "unknown")
    event["final_classification"] = str(event["final_classification"] or "unknown")
    try:
        from log_config import get_logger
        get_logger("proxy").info(json.dumps(event, sort_keys=True, separators=(",", ":")))
        print("[PROXY_TELEMETRY] " + json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True)
    except Exception:
        # Diagnostics must never change proxy/workflow behavior.
        pass
    return event
