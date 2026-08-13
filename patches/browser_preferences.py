#!/usr/bin/env python3
"""Small persistent browser preferences shared by warm-up/workflow modules.

Preferences are profile-scoped and do not rotate the browser fingerprint.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from browser_launcher import active_profile_dir

_ALLOWED_ENGINES = {"google", "bing", "duckduckgo"}


def _path(account: str, mode: str = "desktop") -> Path:
    return active_profile_dir(account, "", mode) / "browser_preferences.json"


def load_browser_preferences(account: str, mode: str = "desktop") -> Dict[str, Any]:
    path = _path(account, mode)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_browser_preferences(account: str, mode: str = "desktop", **updates: Any) -> Dict[str, Any]:
    path = _path(account, mode)
    data = load_browser_preferences(account, mode)
    for key, value in updates.items():
        if key in {"preferred_search_engine", "last_working_search_engine"}:
            value = str(value or "").lower()
            if value and value not in _ALLOWED_ENGINES:
                continue
        data[key] = value
    data.setdefault("preferred_search_engine", "google")
    data["updated_at"] = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    return data


def preferred_search_engine(account: str, mode: str = "desktop") -> str:
    value = str(load_browser_preferences(account, mode).get("preferred_search_engine") or "google").lower()
    return value if value in _ALLOWED_ENGINES else "google"
