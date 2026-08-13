#!/usr/bin/env python3
"""Persistent Camoufox/BrowserForge fingerprint bundle (Runtime v2.4.2).

This runtime stores the *full* BrowserForge fingerprint once per account and
reuses that exact dataclass on every launch.  It avoids asking Camoufox to
regenerate a new BrowserForge fingerprint from an exact screen constraint on
every start, which can fail with:

    No headers based on this input can be generated

The proxy is deliberately excluded from the fingerprint seed.  Existing
legacy fingerprints are upgraded in place: account storage and OS family are
preserved, while stale/incomplete UA/header data is rebuilt for the installed
Camoufox Firefox version.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import random
import re
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, Optional

FINGERPRINT_FILENAME = "camoufox_fingerprint.json"
FINGERPRINT_SCHEMA_VERSION = 6
BROWSER_LOCALE = "en-US"


def _seed_int(account: str, profile_dir: Path) -> int:
    identity = "%s|%s" % (str(account), str(profile_dir.name or "default"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _host_os() -> str:
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name == "windows":
        return "windows"
    return "linux"


def _detect_config_os(config: Dict[str, Any], fallback: str) -> str:
    blob = " ".join(
        str(config.get(key) or "").lower()
        for key in ("navigator.platform", "navigator.oscpu", "navigator.userAgent")
    )
    if "mac" in blob:
        return "macos"
    if "win" in blob:
        return "windows"
    if "linux" in blob or "x11" in blob:
        return "linux"
    return fallback


def _default_geometry() -> Dict[str, Any]:
    return {
        "preset": "windows_large_1680x1050_v6",
        "screen": {"width": 1680, "height": 1050, "avail_width": 1680, "avail_height": 1010},
        "viewport": {"width": 1608, "height": 887},
        "outer": {"width": 1624, "height": 975},
        "position": {"x": 28, "y": 18},
        "device_scale_factor": 1,
    }


def _installed_firefox_major() -> Optional[str]:
    try:
        from camoufox.pkgman import installed_verstr  # type: ignore

        raw = str(installed_verstr() or "")
        match = re.search(r"(?<!\d)(1\d{2})(?!\d)", raw)
        if match:
            return match.group(1)
        first = raw.split(".", 1)[0]
        return first if first.isdigit() else None
    except Exception:
        return None


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except Exception:
        pass


def _backup_legacy(path: Path) -> None:
    if not path.exists():
        return
    backup = path.with_name("camoufox_fingerprint.pre_v241.backup.json")
    if backup.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.pop("proxy", None)
            _atomic_write(backup, payload)
    except Exception:
        pass


def _generate_browserforge_dict(account: str, profile_dir: Path, preferred_os: str) -> Optional[Dict[str, Any]]:
    """Generate a complete BrowserForge fingerprint with relaxed screen rules.

    Exact screen constraints are intentionally not supplied here.  Geometry is
    applied to the generated fingerprint afterwards and then persisted.
    """
    try:
        from camoufox.fingerprints import generate_fingerprint  # type: ignore
    except Exception:
        return None

    requested_os = preferred_os if preferred_os in {"macos", "windows", "linux"} else _host_os()
    base_seed = _seed_int(account, profile_dir)
    py_state = random.getstate()
    np_state = None
    try:
        try:
            import numpy as np  # type: ignore

            np_state = np.random.get_state()
        except Exception:
            np_state = None

        for attempt in range(16):
            seed = base_seed + attempt * 104729
            random.seed(seed)
            if np_state is not None:
                try:
                    import numpy as np  # type: ignore

                    np.random.seed(seed % (2**32))
                except Exception:
                    pass
            try:
                fp = generate_fingerprint(os=requested_os)
                data = asdict(fp)
            except Exception:
                continue
            nav = data.get("navigator") if isinstance(data, dict) else None
            if not isinstance(nav, dict):
                continue
            probe = {
                "navigator.platform": nav.get("platform"),
                "navigator.oscpu": nav.get("oscpu"),
                "navigator.userAgent": nav.get("userAgent"),
            }
            if _detect_config_os(probe, "") == requested_os:
                return data
        return None
    finally:
        random.setstate(py_state)
        if np_state is not None:
            try:
                import numpy as np  # type: ignore

                np.random.set_state(np_state)
            except Exception:
                pass


def _filter_kwargs(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in payload.items() if k in names}


def _reconstruct_browserforge(data: Dict[str, Any]):
    from browserforge.fingerprints import (  # type: ignore
        Fingerprint,
        NavigatorFingerprint,
        ScreenFingerprint,
        VideoCard,
    )

    screen = ScreenFingerprint(**_filter_kwargs(ScreenFingerprint, dict(data.get("screen") or {})))
    navigator = NavigatorFingerprint(**_filter_kwargs(NavigatorFingerprint, dict(data.get("navigator") or {})))
    video_card_data = data.get("videoCard")
    video_card = None
    if isinstance(video_card_data, dict):
        video_card = VideoCard(**_filter_kwargs(VideoCard, video_card_data))
    kwargs = {
        "screen": screen,
        "navigator": navigator,
        "headers": dict(data.get("headers") or {}),
        "videoCodecs": dict(data.get("videoCodecs") or {}),
        "audioCodecs": dict(data.get("audioCodecs") or {}),
        "pluginsData": dict(data.get("pluginsData") or {}),
        "battery": data.get("battery"),
        "videoCard": video_card,
        "multimediaDevices": list(data.get("multimediaDevices") or []),
        "fonts": list(data.get("fonts") or []),
        "mockWebRTC": data.get("mockWebRTC"),
        "slim": data.get("slim"),
    }
    return Fingerprint(**_filter_kwargs(Fingerprint, kwargs))


def _apply_geometry_to_bf(data: Dict[str, Any], geometry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    geometry = geometry or _default_geometry()
    result = copy.deepcopy(data)
    screen = result.setdefault("screen", {})
    g_screen = geometry.get("screen") or {}
    viewport = geometry.get("viewport") or {}
    outer = geometry.get("outer") or {}
    position = geometry.get("position") or {}

    width = int(g_screen.get("width") or 1536)
    height = int(g_screen.get("height") or 864)
    avail_width = int(g_screen.get("avail_width") or width)
    avail_height = int(g_screen.get("avail_height") or height)
    inner_width = int(viewport.get("width") or 1440)
    inner_height = int(viewport.get("height") or 712)
    outer_width = int(outer.get("width") or inner_width + 16)
    outer_height = int(outer.get("height") or inner_height + 88)
    dpr = float(geometry.get("device_scale_factor") or screen.get("devicePixelRatio") or 1)

    screen.update(
        {
            "width": width,
            "height": height,
            "availWidth": avail_width,
            "availHeight": avail_height,
            "availLeft": 0,
            "availTop": 0,
            "innerWidth": inner_width,
            "innerHeight": inner_height,
            "outerWidth": outer_width,
            "outerHeight": outer_height,
            "screenX": int(position.get("x") or 0),
            "devicePixelRatio": dpr,
            "clientWidth": inner_width,
            "clientHeight": inner_height,
            "pageXOffset": 0,
            "pageYOffset": 0,
            "colorDepth": int(screen.get("colorDepth") or 24),
            "pixelDepth": int(screen.get("pixelDepth") or 24),
            "hasHDR": bool(screen.get("hasHDR", False)),
        }
    )
    return result


def _apply_legacy_preferences(bf_data: Dict[str, Any], legacy_config: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve stable non-version identity choices from the old partial config."""
    result = copy.deepcopy(bf_data)
    nav = result.setdefault("navigator", {})
    mapping = {
        "navigator.hardwareConcurrency": "hardwareConcurrency",
        "navigator.deviceMemory": "deviceMemory",
        "navigator.doNotTrack": "doNotTrack",
        "navigator.maxTouchPoints": "maxTouchPoints",
    }
    for old_key, new_key in mapping.items():
        if old_key in legacy_config and legacy_config.get(old_key) is not None:
            nav[new_key] = legacy_config.get(old_key)
    nav["language"] = BROWSER_LOCALE
    nav["languages"] = [BROWSER_LOCALE, "en"]
    headers = result.setdefault("headers", {})
    headers["Accept-Language"] = "en-US,en;q=0.9"
    return result


def _resolved_config(fp_obj, geometry: Optional[Dict[str, Any]], seed: int) -> Dict[str, Any]:
    from camoufox.fingerprints import from_browserforge  # type: ignore

    config = from_browserforge(fp_obj, _installed_firefox_major())
    # Deterministic Camoufox-only values which otherwise rotate each launch.
    config["window.history.length"] = 2 + (seed % 4)
    config["fonts:spacing_seed"] = seed % 1_073_741_823
    config["canvas:aaOffset"] = int((seed % 31) - 15)
    config["canvas:aaCapOffset"] = True
    config["headers.Accept-Language"] = "en-US,en;q=0.9"
    config["navigator.language"] = BROWSER_LOCALE
    config["navigator.languages"] = [BROWSER_LOCALE, "en"]
    # BrowserForge already supplies a matching User-Agent.  Keep the explicit
    # network header aligned with navigator.userAgent after Camoufox version
    # normalisation.
    if config.get("navigator.userAgent"):
        config["headers.User-Agent"] = config["navigator.userAgent"]
    return config


def persistent_camoufox_bundle(
    account: str,
    proxy: str,
    profile_dir,
    geometry: Optional[Dict[str, Any]] = None,
    preferred_os: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return a stable BrowserForge object plus its resolved Camoufox config."""
    profile_path = Path(profile_dir)
    profile_path.mkdir(parents=True, exist_ok=True)
    path = profile_path / FINGERPRINT_FILENAME
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except Exception:
            existing = {}

    requested_os = preferred_os if preferred_os in {"macos", "windows", "linux"} else _host_os()
    legacy_config = existing.get("config") if isinstance(existing.get("config"), dict) else {}
    bf_data = existing.get("browserforge_fingerprint") if isinstance(existing.get("browserforge_fingerprint"), dict) else None

    if not bf_data:
        _backup_legacy(path)
        bf_data = _generate_browserforge_dict(account, profile_path, requested_os)
        if not bf_data:
            return None
        bf_data = _apply_legacy_preferences(bf_data, legacy_config)

    bf_data = _apply_geometry_to_bf(bf_data, geometry)
    try:
        fp_obj = _reconstruct_browserforge(bf_data)
    except Exception:
        # BrowserForge dataclasses changed or old data is incomplete. Regenerate
        # one complete fingerprint, preserving only safe legacy preferences.
        _backup_legacy(path)
        bf_data = _generate_browserforge_dict(account, profile_path, requested_os)
        if not bf_data:
            return None
        bf_data = _apply_legacy_preferences(bf_data, legacy_config)
        bf_data = _apply_geometry_to_bf(bf_data, geometry)
        fp_obj = _reconstruct_browserforge(bf_data)

    seed = int(existing.get("seed") or _seed_int(account, profile_path))
    config = _resolved_config(fp_obj, geometry, seed)
    identity_os = _detect_config_os(config, requested_os)
    if not existing and identity_os != requested_os:
        return None

    payload = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "account": account,
        "seed": seed,
        "identity_os": identity_os,
        "created_on_host_os": existing.get("created_on_host_os") or requested_os,
        "identity_validation": "strict_match" if identity_os == requested_os else "legacy_identity_preserved",
        "geometry_preset": (geometry or _default_geometry()).get("preset"),
        "runtime_firefox_major": _installed_firefox_major(),
        "created_at": int(existing.get("created_at") or time.time()),
        "updated_at": int(time.time()),
        "browserforge_fingerprint": bf_data,
        "config": config,
    }
    _atomic_write(path, payload)
    return {
        "fingerprint": fp_obj,
        "config": config,
        "identity_os": identity_os,
        "payload": payload,
    }


def persistent_camoufox_config(
    account: str,
    proxy: str,
    profile_dir,
    geometry: Optional[Dict[str, Any]] = None,
    preferred_os: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    bundle = persistent_camoufox_bundle(
        account,
        proxy,
        profile_dir,
        geometry=geometry,
        preferred_os=preferred_os,
    )
    return dict(bundle["config"]) if bundle else None


def persistent_browserforge_fingerprint(
    account: str,
    proxy: str,
    profile_dir,
    geometry: Optional[Dict[str, Any]] = None,
    preferred_os: Optional[str] = None,
):
    bundle = persistent_camoufox_bundle(
        account,
        proxy,
        profile_dir,
        geometry=geometry,
        preferred_os=preferred_os,
    )
    return bundle["fingerprint"] if bundle else None
