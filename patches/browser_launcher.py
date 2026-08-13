#!/usr/bin/env python3
"""Unified SparkBrowser launcher for Instagram web profiles.

Runtime v2.2 principles:
- one persistent identity per account and browser mode;
- proxy is a connection setting, never part of the fingerprint seed;
- host OS and geometry are selected once when a profile is created;
- proxy location requires Camoufox GeoIP with no host-location fallback;
- Instagram/browser language stays en-US; direct connections use host timezone;
- browser cache and profile storage are preserved between launches.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import locale as _locale
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse
from browser_page_router import attach_page_router

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ["SPARKGRID_DATA_DIR"]) if os.environ.get("SPARKGRID_DATA_DIR") else ROOT
PROFILE_ROOT = DATA_ROOT / "browser_profiles" / "ig_web_upload"
PROFILE_SCHEMA_VERSION = 6
GEOMETRY_SCHEMA_VERSION = 6
BROWSER_LOCALE = "en-US"


def _configure_camoufox_runtime_assets() -> Dict[str, str]:
    """Apply the explicit runtime contract in every fresh worker process."""
    browser_value = str(os.environ.get("SPARKGRID_CAMOUFOX_DIR") or "").strip()
    geoip_value = str(os.environ.get("SPARKGRID_GEOIP_PATH") or "").strip()
    configured: Dict[str, str] = {}

    if browser_value:
        browser_dir = Path(browser_value).expanduser().resolve()
        executable = browser_dir / ("camoufox.exe" if os.name == "nt" else "camoufox")
        version_file = browser_dir / "version.json"
        if not executable.is_file():
            raise RuntimeError("SparkBrowser runtime executable is missing: %s" % executable)
        if not version_file.is_file():
            raise RuntimeError("SparkBrowser runtime version metadata is missing: %s" % version_file)
        import camoufox.pkgman as pkgman  # type: ignore

        pkgman.INSTALL_DIR = browser_dir
        configured["browser_dir"] = str(browser_dir)
        configured["browser_executable"] = str(executable)

    if geoip_value:
        geoip_path = Path(geoip_value).expanduser().resolve()
        if not geoip_path.is_file():
            raise RuntimeError("SparkBrowser GeoIP database is missing: %s" % geoip_path)
        import camoufox.locale as camoufox_locale  # type: ignore

        camoufox_locale.MMDB_FILE = geoip_path
        configured["geoip_path"] = str(geoip_path)

    return configured


class ProxyConfigurationError(ValueError):
    """A configured proxy cannot be normalized for browser use.

    The message is deliberately a stable code: proxy input can contain
    credentials and must never be copied into workflow diagnostics.
    """

    classification = "proxy_parse_error"

    def __init__(self):
        super().__init__(self.classification)


class BrowserProxyApplicationError(RuntimeError):
    """The browser rejected an otherwise normalized proxy configuration."""

    classification = "browser_proxy_application_failed"

    def __init__(self):
        super().__init__(self.classification)

try:
    import geoip2  # type: ignore  # noqa: F401
    _GEOIP_OK = True
except Exception:
    _GEOIP_OK = False

# Fallback desktop geometries. On a real desktop host Runtime v2.2 measures the
# primary display once when the profile is created and stores a large, coherent
# non-maximized window preset. The stored geometry is then reused unchanged.
HOST_DESKTOP_GEOMETRIES: Dict[str, Dict[str, Any]] = {
    "macos": {
        "preset": "macos_large_1512x982_v5",
        "screen": {"width": 1512, "height": 982, "avail_width": 1512, "avail_height": 944},
        "viewport": {"width": 1408, "height": 804},
        "outer": {"width": 1424, "height": 896},
        "position": {"x": 44, "y": 28},
        "device_scale_factor": 2,
    },
    "windows": {
        "preset": "windows_large_1680x1050_v6",
        "screen": {"width": 1680, "height": 1050, "avail_width": 1680, "avail_height": 1010},
        "viewport": {"width": 1608, "height": 887},
        "outer": {"width": 1624, "height": 975},
        "position": {"x": 28, "y": 18},
        "device_scale_factor": 1,
    },
    "linux": {
        "preset": "linux_large_1440x900_v5",
        "screen": {"width": 1440, "height": 900, "avail_width": 1440, "avail_height": 860},
        "viewport": {"width": 1344, "height": 736},
        "outer": {"width": 1360, "height": 824},
        "position": {"x": 40, "y": 18},
        "device_scale_factor": 1,
    },
}
DESKTOP_GEOMETRY: Dict[str, Any] = HOST_DESKTOP_GEOMETRIES["macos"]
MOBILE_GEOMETRY: Dict[str, Any] = {
    "preset": "stable_mobile_390x844_v3",
    "screen": {"width": 390, "height": 844, "avail_width": 390, "avail_height": 844},
    "viewport": {"width": 390, "height": 844},
    "outer": {"width": 390, "height": 844},
    "position": {"x": 0, "y": 0},
    "device_scale_factor": 3,
}
DEFAULT_DESKTOP_VIEWPORTS = (dict(DESKTOP_GEOMETRY["viewport"]),)
MOBILE_LIKE_VIEWPORT = dict(MOBILE_GEOMETRY["viewport"])


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip().lstrip("@"))[:90] or "account"


def _default_proxy_scheme() -> str:
    scheme = str(os.environ.get("SPARKGRID_PROXY_SCHEME") or "http").strip().lower()
    return scheme if scheme in {"http", "https", "socks4", "socks5", "socks5h"} else "http"


def parse_proxy_for_browser(proxy: str) -> Optional[Dict[str, str]]:
    """Parse common HTTP/HTTPS/SOCKS proxy formats for Playwright/Camoufox.

    Legacy authentication is deliberately left-bounded as
    ``host:port:username:password-with-optional-colons``.  Username characters
    that make that representation ambiguous must use a URL-form proxy instead.
    """
    raw = str(proxy or "").strip()
    if not raw:
        return None

    normalized = raw
    legacy_credentials: Optional[Tuple[str, str]] = None
    if "://" not in normalized:
        # user:password@host:port
        if "@" in normalized:
            normalized = _default_proxy_scheme() + "://" + normalized
        else:
            parts = normalized.split(":", 2)
            if len(parts) == 3 and parts[0] and parts[1].isdigit():
                # host:port:username:password-with-any-remaining-colons
                host, port, auth = parts
                user, separator, password = auth.partition(":")
                if separator and user and password:
                    normalized = "%s://%s:%s" % (_default_proxy_scheme(), host, port)
                    legacy_credentials = (user, password)
                else:
                    normalized = "%s://%s" % (_default_proxy_scheme(), normalized)
            elif len(parts) == 2 and parts[1].isdigit():
                normalized = "%s://%s" % (_default_proxy_scheme(), normalized)
            else:
                normalized = "%s://%s" % (_default_proxy_scheme(), normalized)

    try:
        parsed = urlparse(normalized)
        scheme = (parsed.scheme or _default_proxy_scheme()).lower()
        if scheme not in {"http", "https", "socks4", "socks5", "socks5h"}:
            raise ValueError("unsupported proxy scheme: %s" % scheme)
        if not parsed.hostname or not parsed.port:
            raise ValueError("proxy must contain host and port")
        out: Dict[str, str] = {"server": "%s://%s:%s" % (scheme, parsed.hostname, parsed.port)}
        if legacy_credentials is not None:
            out["username"], out["password"] = legacy_credentials
        elif parsed.username is not None:
            out["username"] = unquote(parsed.username)
        if legacy_credentials is None and parsed.password is not None:
            out["password"] = unquote(parsed.password)
        return out
    except Exception:
        # Keep compatibility with previously accepted values. Camoufox will
        # return the actionable launch error if the value itself is malformed.
        return {"server": raw}


def proxy_signature(proxy: str) -> str:
    raw = str(proxy or "").strip()
    if not raw:
        return "direct"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def account_profile_root(account: str, mode: str = "desktop") -> Path:
    path = PROFILE_ROOT / safe_name(account) / mode
    path.mkdir(parents=True, exist_ok=True)
    return path


def _profile_host_os(path: Path) -> str:
    """Read the OS identity stored in a profile without creating/migrating it."""
    try:
        payload = json.loads((Path(path) / "sparkbrowser_profile.json").read_text(encoding="utf-8"))
        value = str(payload.get("created_on_host_os") or payload.get("identity_os") or payload.get("identity_os_preference") or "").lower()
        return value if value in {"macos", "windows", "linux"} else ""
    except Exception:
        return ""


def active_profile_dir(account: str, proxy: str = "", mode: str = "desktop", create: bool = True) -> Path:
    """Return a persistent profile compatible with the current operating system.

    A copied project may contain a macOS profile and later run on Windows. Those
    browser identities must not be merged. SparkGrid therefore keeps the legacy
    ``default`` profile on its original OS and creates ``default_windows`` (or
    ``default_linux``/``default_macos``) beside it when needed.
    """
    root = account_profile_root(account, mode) / "profiles"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    host_os = _host_identity_os()
    default = root / "default"
    host_specific = root / ("default_" + host_os)

    if host_specific.exists():
        path = host_specific
    elif default.exists():
        stored_os = _profile_host_os(default)
        path = default if not stored_os or stored_os == host_os else host_specific
    else:
        compatible = [item for item in root.glob("*") if item.is_dir() and _profile_host_os(item) in {"", host_os}]
        legacy_direct = root / "direct"
        if legacy_direct in compatible:
            path = legacy_direct
        elif compatible:
            path = max(compatible, key=lambda item: item.stat().st_mtime)
        else:
            path = default
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def storage_state_path(account: str, proxy: str = "", mode: str = "desktop") -> Path:
    return active_profile_dir(account, proxy, mode) / "camoufox_storage_state.json"


def legacy_storage_state_path(account: str, mode: str = "desktop") -> Path:
    legacy = account_profile_root(account, mode) / "camoufox_storage_state.json"
    # Do not silently import an old cross-OS storage state into a newly created
    # Windows/macOS/Linux identity. The native profile will save a fresh state
    # after login on this computer.
    active = active_profile_dir(account, "", mode)
    if active.name.startswith("default_") and active.name != "default":
        return active / ".legacy_storage_state_disabled.json"
    return legacy


def _metadata_path(account: str, proxy: str = "", mode: str = "desktop") -> Path:
    return active_profile_dir(account, proxy, mode) / "sparkbrowser_profile.json"


def _runtime_report_path(account: str, proxy: str = "", mode: str = "desktop") -> Path:
    return active_profile_dir(account, proxy, mode) / "sparkbrowser_runtime.json"


def _host_identity_os() -> str:
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name == "windows":
        return "windows"
    return "linux"


def _normalise_locale(value: str) -> str:
    value = str(value or "").strip().split(".", 1)[0].replace("_", "-")
    if not value or value.upper() in {"C", "POSIX"}:
        return "en-US"
    parts = value.split("-")
    if len(parts) == 1:
        return parts[0].lower()
    return parts[0].lower() + "-" + parts[1].upper()


def _system_locale() -> str:
    for candidate in (
        os.environ.get("LC_ALL"),
        os.environ.get("LC_MESSAGES"),
        os.environ.get("LANG"),
    ):
        if candidate and str(candidate).upper() not in {"C", "POSIX"}:
            return _normalise_locale(str(candidate))
    try:
        current = _locale.getlocale()[0]
        if current:
            return _normalise_locale(current)
    except Exception:
        pass
    return "en-US"


def _system_timezone() -> str:
    explicit = str(os.environ.get("TZ") or "").strip()
    if explicit and "/" in explicit:
        return explicit
    try:
        localtime = Path("/etc/localtime")
        if localtime.is_symlink():
            target = os.path.realpath(str(localtime))
            marker = "/zoneinfo/"
            if marker in target:
                return target.split(marker, 1)[1]
    except Exception:
        pass
    try:
        tzinfo = datetime.now().astimezone().tzinfo
        key = getattr(tzinfo, "key", "")
        if key and "/" in str(key):
            return str(key)
    except Exception:
        pass
    return ""


def _host_display_bounds(identity_os: str = "") -> Optional[Dict[str, int]]:
    """Measure the primary logical display without keeping a runtime dependency.

    The value is used only while a new geometry preset is created/migrated. It is
    stored in sparkbrowser_profile.json and is not re-read on every launch.
    """
    identity_os = identity_os or _host_identity_os()
    try:
        if identity_os == "macos":
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "Finder" to get bounds of window of desktop',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            nums = [int(x) for x in re.findall(r"-?\d+", result.stdout or "")]
            if len(nums) >= 4:
                left, top, right, bottom = nums[:4]
                width, height = right - left, bottom - top
                if width >= 1024 and height >= 700:
                    return {"width": width, "height": height, "avail_width": width, "avail_height": max(700, height - 38)}
        elif identity_os == "windows":
            import ctypes  # type: ignore
            width = int(ctypes.windll.user32.GetSystemMetrics(0))
            height = int(ctypes.windll.user32.GetSystemMetrics(1))
            if width >= 1024 and height >= 700:
                return {"width": width, "height": height, "avail_width": width, "avail_height": max(700, height - 40)}
    except Exception:
        pass
    return None


def _largest_windows_screen_for_host(bounds: Optional[Dict[str, int]]) -> Dict[str, int]:
    """Choose a common Windows logical screen that fits the physical host.

    Legacy Windows identities may run on a Mac host.  Using the Mac's exact
    1710x1107 screen would mix OS-specific geometry, while a fixed 1536x864
    window is unnecessarily small.  We therefore snap once to the largest
    common Windows resolution that physically fits the host display.
    """
    standards = (
        (1920, 1080),
        (1680, 1050),
        (1600, 900),
        (1536, 864),
        (1440, 900),
        (1366, 768),
    )
    if bounds:
        max_w = int(bounds.get("avail_width") or bounds.get("width") or 0)
        max_h = int(bounds.get("avail_height") or bounds.get("height") or 0) + 40
        for width, height in standards:
            if width <= max_w and height <= max_h:
                return {"width": width, "height": height}
    return {"width": 1536, "height": 864}


def _large_desktop_geometry(identity_os: str = "") -> Dict[str, Any]:
    identity_os = identity_os or _host_identity_os()
    fallback = json.loads(json.dumps(HOST_DESKTOP_GEOMETRIES.get(identity_os, HOST_DESKTOP_GEOMETRIES["linux"])))

    # Measure the real host so the native window always fits.  The browser-facing
    # screen remains aligned to the stored identity OS.
    host_bounds = _host_display_bounds(_host_identity_os())
    identity_bounds = _host_display_bounds(identity_os)

    if identity_os == "windows":
        chosen = _largest_windows_screen_for_host(host_bounds)
        width, height = int(chosen["width"]), int(chosen["height"])
        avail_width, avail_height = width, max(700, height - 40)
        outer_width = max(1180, min(avail_width - 56, round(avail_width * 0.967)))
        outer_height = max(780, min(avail_height - 24, round(avail_height * 0.965)))
        viewport_width = outer_width - 16
        viewport_height = outer_height - 88
        x = max(12, (width - outer_width) // 2)
        y = 18
        return {
            "preset": "windows_large_%sx%s_v6" % (width, height),
            "screen": {"width": width, "height": height, "avail_width": avail_width, "avail_height": avail_height},
            "viewport": {"width": viewport_width, "height": viewport_height},
            "outer": {"width": outer_width, "height": outer_height},
            "position": {"x": x, "y": y},
            "device_scale_factor": 1,
            "source": "windows_standard_snapped_to_host_capacity",
        }

    bounds = identity_bounds or host_bounds
    if not bounds:
        return fallback

    width = int(bounds["width"])
    height = int(bounds["height"])
    avail_width = int(bounds.get("avail_width") or width)
    avail_height = int(bounds.get("avail_height") or height)

    # Use almost the full display, but never maximize. This keeps native window
    # state, JS screen metrics and Playwright viewport coherent.
    margin_x = max(18, min(48, round(avail_width * 0.025)))
    margin_top = 24 if identity_os == "macos" else 14
    margin_bottom = 14
    outer_width = max(1180, min(avail_width - 2 * margin_x, round(avail_width * 0.96)))
    outer_height = max(780, min(avail_height - margin_top - margin_bottom, round(avail_height * 0.95)))
    chrome_height = 92 if identity_os in {"macos", "linux"} else 88
    viewport_width = max(1100, outer_width - 16)
    viewport_height = max(680, outer_height - chrome_height)
    x = max(8, (avail_width - outer_width) // 2)
    y = max(8, margin_top)
    dpr = 2 if identity_os == "macos" else 1
    return {
        "preset": "%s_host_%sx%s_large_v6" % (identity_os, width, height),
        "screen": {"width": width, "height": height, "avail_width": avail_width, "avail_height": avail_height},
        "viewport": {"width": viewport_width, "height": viewport_height},
        "outer": {"width": outer_width, "height": outer_height},
        "position": {"x": x, "y": y},
        "device_scale_factor": dpr,
        "source": "host_primary_display_at_profile_creation",
    }


def _geometry_for_mode(mode: str, identity_os: str = "") -> Dict[str, Any]:
    if mode == "mobile_like":
        return json.loads(json.dumps(MOBILE_GEOMETRY))
    return _large_desktop_geometry(identity_os or _host_identity_os())


def _geometry_needs_v6_upgrade(
    previous: Dict[str, Any],
    geometry: Optional[Dict[str, Any]],
    mode: str,
    identity_os: str,
) -> bool:
    """Upgrade geometry when it is stale or inconsistent with the stored OS identity.

    The native host is used only when a NEW identity is created. Existing profiles
    keep their previously observed identity OS, and their screen/DPR geometry is
    aligned to that identity instead of silently switching to the current host.
    """
    if mode == "mobile_like" or not isinstance(geometry, dict):
        return geometry is None
    if bool(previous.get("geometry_user_locked")):
        return False
    preset = str(geometry.get("preset") or "").lower()
    viewport = geometry.get("viewport") if isinstance(geometry.get("viewport"), dict) else {}
    try:
        vw, vh = int(viewport.get("width") or 0), int(viewport.get("height") or 0)
        dpr = int(geometry.get("device_scale_factor") or previous.get("device_scale_factor") or 0)
    except Exception:
        vw, vh, dpr = 0, 0, 0
    identity_os = str(identity_os or "").lower()
    os_mismatch = (
        (identity_os == "windows" and ("macos" in preset or dpr >= 2))
        or (identity_os == "macos" and ("windows" in preset or (dpr and dpr < 2)))
        or (identity_os == "linux" and ("macos" in preset or "windows" in preset))
    )
    return (
        int(previous.get("geometry_schema_version") or 0) < GEOMETRY_SCHEMA_VERSION
        or preset.endswith("_v2")
        or preset.endswith("_v3")
        or preset.endswith("_v4")
        or (vw <= 1280 and vh <= 720)
        or os_mismatch
    )


def _headers_for_locale(locale: str) -> Dict[str, str]:
    primary = BROWSER_LOCALE
    family = primary.split("-", 1)[0]
    return {"Accept-Language": "%s,%s;q=0.9,en;q=0.8" % (primary, family)}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, payload: Dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
    os.replace(str(tmp), str(path))
    if private:
        try:
            os.chmod(str(path), 0o600)
        except Exception:
            pass


def _existing_fingerprint_identity(account: str, proxy: str, mode: str) -> str:
    """Read the already persisted fingerprint OS before choosing geometry."""
    path = active_profile_dir(account, proxy, mode) / "camoufox_fingerprint.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ""
        config = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        declared = str(payload.get("identity_os") or "").lower()
        if declared in {"macos", "windows", "linux"}:
            return declared
    except Exception:
        return ""
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
    return ""


def ensure_profile_metadata(
    account: str,
    proxy: str = "",
    mode: str = "desktop",
    locale: str = "",
) -> Dict[str, Any]:
    """Create or migrate the stable per-account runtime metadata."""
    path = _metadata_path(account, proxy, mode)
    previous = _read_json(path)
    now = int(time.time())
    host_os = _host_identity_os()
    created_on_host_os = str(previous.get("created_on_host_os") or host_os)
    # Existing profiles must follow the OS identity already stored in their
    # fingerprint/metadata. The host OS is used only for a genuinely new profile.
    identity_os_preference = str(
        previous.get("identity_os")
        or _existing_fingerprint_identity(account, proxy, mode)
        or previous.get("identity_os_preference")
        or created_on_host_os
        or host_os
    )
    direct_locale = BROWSER_LOCALE
    direct_timezone = _system_timezone()
    previous_geometry = previous.get("geometry") if isinstance(previous.get("geometry"), dict) else None
    if _geometry_needs_v6_upgrade(previous, previous_geometry, mode, identity_os_preference):
        geometry = _geometry_for_mode(mode, identity_os_preference)
        geometry_migrated = bool(previous_geometry)
    else:
        geometry = previous_geometry or _geometry_for_mode(mode, identity_os_preference)
        geometry_migrated = False

    data: Dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "account": account,
        "mode": mode,
        "profile_identity": previous.get("profile_identity")
        or hashlib.sha256((str(account) + "|" + str(mode)).encode("utf-8")).hexdigest()[:16],
        "created_on_host_os": created_on_host_os,
        "identity_os_preference": identity_os_preference,
        "current_host_os": host_os,
        "host_os_locked": True,
        "preferred_locale": direct_locale,
        # Compatibility aliases consumed by older modules.
        "locale": direct_locale,
        "timezone_id": direct_timezone,
        "geometry": geometry,
        "geometry_preset": geometry["preset"],
        "geometry_schema_version": GEOMETRY_SCHEMA_VERSION,
        "geometry_source": geometry.get("source") or "host_os_fallback",
        "geometry_migrated_to_identity_v6": geometry_migrated,
        "viewport": dict(geometry["viewport"]),
        "screen": {
            "width": geometry["screen"]["width"],
            "height": geometry["screen"]["height"],
        },
        "window_outer": dict(geometry["outer"]),
        "device_scale_factor": int(geometry.get("device_scale_factor") or (3 if mode == "mobile_like" else (2 if identity_os_preference == "macos" else 1))),
        "is_mobile": mode == "mobile_like",
        "current_proxy_signature": proxy_signature(proxy),
        "proxy_present": bool(str(proxy or "").strip()),
        "location_policy": "proxy_geoip_required" if proxy else "system_direct",
        "browser_locale_policy": "forced_english",
        "cross_os_policy": "blocked_by_default",
        "created_at": int(previous.get("created_at") or now),
        "updated_at": now,
        "migrated_from_schema": int(previous.get("schema_version") or 1) if previous else 0,
    }
    _atomic_write_json(path, data)
    return data


def _int_cfg(config: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(config.get(key) if config.get(key) is not None else default)
    except Exception:
        return int(default)


def _detect_identity_os(config: Dict[str, Any], fallback: str) -> str:
    platform_value = str(config.get("navigator.platform") or "").lower()
    oscpu = str(config.get("navigator.oscpu") or "").lower()
    ua = str(config.get("navigator.userAgent") or "").lower()
    blob = " ".join((platform_value, oscpu, ua))
    if "mac" in blob:
        return "macos"
    if "win" in blob:
        return "windows"
    if "linux" in blob or "x11" in blob:
        return "linux"
    return fallback


def _fingerprint_bundle(account: str, profile_dir: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    try:
        _configure_camoufox_runtime_assets()
        from web_fingerprint import persistent_camoufox_bundle  # type: ignore
        bundle = persistent_camoufox_bundle(
            account,
            "",  # proxy must never alter the identity seed
            profile_dir,
            geometry=meta.get("geometry"),
            preferred_os=meta.get("identity_os_preference"),
        )
    except Exception as exc:
        raise RuntimeError(
            "SparkBrowser fingerprint helper failed: %s: %s"
            % (type(exc).__name__, exc)
        ) from exc
    if not isinstance(bundle, dict) or not bundle.get("config") or bundle.get("fingerprint") is None:
        raise RuntimeError("SparkBrowser fingerprint could not be created; refusing random browser fallback")
    return bundle


def _fingerprint_config(account: str, profile_dir: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Backward-compatible helper used by diagnostics/tests."""
    return dict(_fingerprint_bundle(account, profile_dir, meta)["config"])


def _runtime_location(meta: Dict[str, Any], proxy: str, explicit_locale: str = "") -> Dict[str, Any]:
    # Instagram UI remains English. Location is independent from language.
    if proxy:
        if not _GEOIP_OK:
            raise RuntimeError(
                "A proxy is configured but SparkBrowser GeoIP support is unavailable. "
                "The installed SparkBrowser runtime may be incomplete; host location fallback is disabled."
            )
        return {
            "source": "proxy_geoip",
            "locale": BROWSER_LOCALE,
            "timezone_id": "",
            "geoip": True,
            "host_location_fallback": False,
        }
    return {
        "source": "system_direct",
        "locale": BROWSER_LOCALE,
        "timezone_id": _system_timezone(),
        "geoip": False,
        "host_location_fallback": False,
    }


def _assert_host_compatibility(meta: Dict[str, Any]) -> None:
    created = str(meta.get("created_on_host_os") or meta.get("identity_os_preference") or "")
    current = _host_identity_os()
    if created and created != current and os.environ.get("SPARKGRID_ALLOW_CROSS_OS_PROFILE") != "1":
        raise RuntimeError(
            "This browser profile was created on %s but is being opened on %s. "
            "Cross-OS launch is blocked to avoid changing the browser/device identity. "
            "Create a separate profile on this OS or set SPARKGRID_ALLOW_CROSS_OS_PROFILE=1 explicitly."
            % (created, current)
        )


def _validate_proxy_for_profile(proxy: str) -> Optional[Dict[str, str]]:
    if not str(proxy or "").strip():
        return None
    parsed = parse_proxy_for_browser(proxy)
    if not parsed or not parsed.get("server"):
        raise ProxyConfigurationError()
    server = str(parsed.get("server") or "")
    try:
        checked = urlparse(server if "://" in server else (_default_proxy_scheme() + "://" + server))
        if checked.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not checked.hostname or not checked.port:
            raise ProxyConfigurationError()
    except ProxyConfigurationError:
        raise
    except (TypeError, ValueError):
        raise ProxyConfigurationError() from None
    if ("username" in parsed or "password" in parsed) and not (parsed.get("username") and parsed.get("password")):
        raise ProxyConfigurationError()
    if not _GEOIP_OK:
        raise RuntimeError(
            "Proxy profiles require SparkBrowser GeoIP support. Reinstall SparkGrid to restore the bundled browser runtime."
        )
    return parsed


def get_profile_runtime(
    account: str,
    proxy: str = "",
    mode: str = "desktop",
    locale: str = "",
) -> Dict[str, Any]:
    """Public helper used by Chromium fallback paths and diagnostics."""
    meta = ensure_profile_metadata(account, proxy, mode, locale=locale)
    runtime = _runtime_location(meta, proxy, explicit_locale=locale)
    result = dict(meta)
    result["runtime_location"] = runtime
    result["locale"] = runtime.get("locale") or meta.get("preferred_locale") or "en-US"
    result["timezone_id"] = runtime.get("timezone_id") or ""
    return result


@contextlib.contextmanager
def _profile_creation_lock(account: str, mode: str):
    root = account_profile_root(account, mode)
    lock = root / ".sparkgrid_profile_create.lock"
    fd = None
    try:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, (str(os.getpid()) + "\n").encode("utf-8"))
        except FileExistsError as exc:
            try:
                age = time.time() - lock.stat().st_mtime
            except Exception:
                age = 0
            if age > 300:
                try:
                    lock.unlink()
                except Exception:
                    pass
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, (str(os.getpid()) + "\n").encode("utf-8"))
            else:
                raise RuntimeError("Profile creation is already running for this account") from exc
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            lock.unlink()
        except Exception:
            pass


def create_spark_profile(account: str, proxy: str = "", mode: str = "desktop") -> Dict[str, Any]:
    """Create a complete, deterministic profile without opening Instagram.

    Creation writes metadata and the actual Camoufox fingerprint immediately,
    validates host-OS coherence, proxy format and GeoIP availability, and is
    safe to call repeatedly.
    """
    with _profile_creation_lock(account, mode):
        _validate_proxy_for_profile(proxy)
        metadata = ensure_profile_metadata(account, proxy, mode, locale=BROWSER_LOCALE)
        _assert_host_compatibility(metadata)
        profile_dir = active_profile_dir(account, proxy, mode)
        fingerprint_path = profile_dir / "camoufox_fingerprint.json"
        existed_before = fingerprint_path.exists()
        bundle = _fingerprint_bundle(account, profile_dir, metadata)
        config = dict(bundle["config"])
        detected_os = str(bundle.get("identity_os") or _detect_identity_os(
            config, str(metadata.get("identity_os_preference") or _host_identity_os())
        ))
        expected_os = str(metadata.get("identity_os_preference") or _host_identity_os())
        if not existed_before and detected_os != expected_os:
            raise RuntimeError(
                "New fingerprint OS mismatch: expected %s, generated %s" % (expected_os, detected_os)
            )
        metadata["identity_os"] = detected_os
        metadata["identity_validation"] = (
            "strict_match" if detected_os == expected_os else "legacy_identity_preserved"
        )
        metadata["profile_created_complete"] = True
        metadata["updated_at"] = int(time.time())
        _atomic_write_json(_metadata_path(account, proxy, mode), metadata)
        _atomic_write_json(profile_dir / "sparkbrowser_creation_report.json", {
            "schema_version": 1,
            "account": account,
            "mode": mode,
            "profile_dir": str(profile_dir),
            "created_on_host_os": metadata.get("created_on_host_os"),
            "identity_os": detected_os,
            "identity_validation": metadata.get("identity_validation"),
            "geometry_preset": metadata.get("geometry_preset"),
            "proxy_signature": proxy_signature(proxy),
            "location_policy": metadata.get("location_policy"),
            "locale": BROWSER_LOCALE,
            "created_at": int(time.time()),
        })
        return metadata


def _camoufox_screen(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    width = int(value.get("width") or 0)
    height = int(value.get("height") or 0)
    if width <= 0 or height <= 0:
        return value
    try:
        from browserforge.fingerprints import Screen  # type: ignore
        return Screen(min_width=width, max_width=width, min_height=height, max_height=height)
    except Exception:
        return value


def _build_launch_kwargs(
    account: str,
    proxy: str,
    mode: str,
    headless: bool,
    locale: str = "",
    humanize: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    proxy_cfg = _validate_proxy_for_profile(proxy)
    profile_dir = active_profile_dir(account, proxy, mode)
    meta = ensure_profile_metadata(account, proxy, mode, locale=BROWSER_LOCALE)
    _assert_host_compatibility(meta)
    bundle = _fingerprint_bundle(account, profile_dir, meta)
    fingerprint_config = dict(bundle["config"])
    browserforge_fingerprint = bundle["fingerprint"]
    identity_os = str(bundle.get("identity_os") or _detect_identity_os(
        fingerprint_config, str(meta.get("identity_os_preference") or _host_identity_os())
    ))
    meta["identity_os"] = identity_os
    meta["device_scale_factor"] = int(meta.get("geometry", {}).get("device_scale_factor") or (3 if mode == "mobile_like" else (2 if identity_os == "macos" else 1)))
    location = _runtime_location(meta, proxy, explicit_locale=BROWSER_LOCALE)
    meta["runtime_location"] = location
    _atomic_write_json(_metadata_path(account, proxy, mode), meta)

    geometry = meta["geometry"]
    launch: Dict[str, Any] = {
        "headless": bool(headless),
        "persistent_context": True,
        "user_data_dir": str(profile_dir),
        "config": fingerprint_config,
        "fingerprint": browserforge_fingerprint,
        "i_know_what_im_doing": True,
        "humanize": bool(humanize),
        "block_webrtc": True,
        "enable_cache": True,
        "accept_downloads": True,
        "viewport": dict(geometry["viewport"]),
        "device_scale_factor": int(meta["device_scale_factor"]),
        "locale": location["locale"],
    }
    if mode == "mobile_like":
        launch.update({"is_mobile": True, "has_touch": True})
    if location.get("timezone_id"):
        launch["timezone_id"] = location["timezone_id"]
    if proxy_cfg:
        launch["proxy"] = proxy_cfg
        if location.get("geoip"):
            launch["geoip"] = True
    # Diagnosed 2026-08-13: on Windows VPS with KVM/QEMU (Red Hat VirtIO GPU)
    # the browser freezes indefinitely when the RDP session disconnects —
    # the GPU rendering context dies and Playwright/Camoufox blocks on
    # page.evaluate/poll forever (8s timeout ignored, 3h52m hang observed).
    # Forcing software rendering via Firefox prefs makes the browser render
    # via CPU (SwiftShader), independent of the RDP display adapter.
    launch["firefox_user_prefs"] = {
        "gfx.webrender.all": False,
        "gfx.webrender.software": True,
        "layers.acceleration.disabled": True,
        "gfx.direct2d.disabled": True,
        "gfx.direct2d.force-disabled": True,
        "dom.ipc.processCount": 1,
    }
    return launch, meta


def _clear_session_restore(profile_dir: Path) -> None:
    """Remove only stale tab-restore state; preserve cache and browser identity."""
    try:
        for name in ("sessionstore.jsonlz4", "sessionstore.js", "sessionCheckpoints.json"):
            path = Path(profile_dir) / name
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except Exception:
                pass
        backups = Path(profile_dir) / "sessionstore-backups"
        if backups.is_dir():
            for item in backups.iterdir():
                try:
                    if item.is_file() or item.is_symlink():
                        item.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _window_values(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return data.get("chrome://browser/content/browser.xhtml", {}).get("main-window", {}) or {}
    except Exception:
        return {}


def _window_geometry_invalid(current: Dict[str, Any], geometry: Dict[str, Any]) -> bool:
    try:
        width = int(current.get("width") or 0)
        height = int(current.get("height") or 0)
        x = int(current.get("screenX") or 0)
        y = int(current.get("screenY") or 0)
        screen = geometry["screen"]
        if str(current.get("sizemode") or "normal") not in {"normal", ""}:
            return True
        if width < 900 or height < 650:
            return True
        if width > int(screen["avail_width"]) or height > int(screen["avail_height"]):
            return True
        if x < -100 or y < -100 or x >= int(screen["width"]) or y >= int(screen["height"]):
            return True
        expected_outer = geometry.get("outer") or {}
        expected_pos = geometry.get("position") or {}
        # Firefox may persist a manually resized but still technically valid
        # window.  Restore the profile's locked geometry when it drifts enough
        # to become visibly smaller or inconsistent with the fingerprint.
        if abs(width - int(expected_outer.get("width") or width)) > 24:
            return True
        if abs(height - int(expected_outer.get("height") or height)) > 24:
            return True
        if abs(x - int(expected_pos.get("x") or x)) > 48:
            return True
        if abs(y - int(expected_pos.get("y") or y)) > 48:
            return True
        return False
    except Exception:
        return True


def _normalize_window_geometry(profile_dir: Path, geometry: Dict[str, Any]) -> None:
    """Apply the coherent native window preset once, then repair only bad state."""
    profile_dir = Path(profile_dir)
    path = profile_dir / "xulstore.json"
    marker = profile_dir / ".sparkgrid_geometry_v6.json"
    data = _read_json(path)
    current = _window_values(data)
    marker_data = _read_json(marker)
    already_applied = (
        marker_data.get("schema_version") == GEOMETRY_SCHEMA_VERSION
        and marker_data.get("preset") == geometry.get("preset")
    )
    if already_applied and current and not _window_geometry_invalid(current, geometry):
        return

    browser = data.setdefault("chrome://browser/content/browser.xhtml", {})
    window = browser.setdefault("main-window", {})
    outer = geometry["outer"]
    position = geometry["position"]
    window.update({
        "screenX": str(position["x"]),
        "screenY": str(position["y"]),
        "width": str(outer["width"]),
        "height": str(outer["height"]),
        "sizemode": "normal",
    })
    _atomic_write_json(path, data)
    _atomic_write_json(marker, {
        "schema_version": GEOMETRY_SCHEMA_VERSION,
        "preset": geometry.get("preset"),
        "applied_at": int(time.time()),
    })


_TRANSIENT_PROXY_LAUNCH_MARKERS = (
    "failed to connect to proxy",
    "unable to connect to proxy",
    "connection to proxy",
    "proxy connection",
    "connecttimeouterror",
    "connection timed out",
    "connect timeout",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "temporary failure",
)

_PERMANENT_PROXY_LAUNCH_MARKERS = (
    "proxy authentication required",
    "http 407",
    "status 407",
    "unsupported proxy scheme",
    "proxy must contain host and port",
    "invalid proxy",
)


def _is_transient_proxy_launch_error(exc: BaseException) -> bool:
    text = (str(exc) or type(exc).__name__).lower()
    if any(marker in text for marker in _PERMANENT_PROXY_LAUNCH_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_PROXY_LAUNCH_MARKERS)


def _is_proxy_application_error(exc: BaseException) -> bool:
    text = (str(exc) or type(exc).__name__).lower()
    return any(marker in text for marker in (
        *_TRANSIENT_PROXY_LAUNCH_MARKERS,
        *_PERMANENT_PROXY_LAUNCH_MARKERS,
        "proxy application",
        "proxy configuration",
    ))


def _close_partial_camoufox(manager: Any) -> None:
    if manager is None:
        return
    try:
        manager.__exit__(None, None, None)
    except Exception:
        pass


def _enter_camoufox_once(launch: Dict[str, Any]):
    _configure_camoufox_runtime_assets()
    from camoufox.sync_api import Camoufox  # type: ignore

    # Never drop identity-bearing config, persistent path, proxy, locale,
    # timezone, viewport, screen or device scale. Only optional helpers may be
    # removed for compatibility with an older Camoufox runtime.
    optional_drop_order = (
        (),
        ("enable_cache",),
        ("humanize",),
        ("enable_cache", "humanize"),
    )
    last_type_error = None
    for drop in optional_drop_order:
        kwargs = {key: value for key, value in launch.items() if key not in drop}
        manager = None
        try:
            manager = Camoufox(**kwargs)
            context = manager.__enter__()
            return manager, context, kwargs
        except TypeError as exc:
            _close_partial_camoufox(manager)
            last_type_error = exc
            continue
        except Exception as exc:
            _close_partial_camoufox(manager)
            raise RuntimeError("SparkBrowser failed to launch: %s" % exc) from exc
    raise RuntimeError("SparkBrowser launch options are not supported by this browser runtime: %s" % last_type_error)


def _enter_camoufox(launch: Dict[str, Any]):
    proxy_present = bool(launch.get("proxy"))
    try:
        attempts = int(os.environ.get("SPARKGRID_PROXY_LAUNCH_ATTEMPTS") or 3)
    except Exception:
        attempts = 3
    attempts = max(1, min(attempts, 5)) if proxy_present else 1
    try:
        base_delay = float(os.environ.get("SPARKGRID_PROXY_RETRY_BASE_SECONDS") or 4.0)
    except Exception:
        base_delay = 4.0
    base_delay = max(1.0, min(base_delay, 20.0))

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _enter_camoufox_once(launch)
        except Exception as exc:
            last_error = exc
            if proxy_present and _is_proxy_application_error(exc):
                raise BrowserProxyApplicationError() from exc
            if attempt >= attempts or not _is_transient_proxy_launch_error(exc):
                raise
            # Same static endpoint often recovers on the next gateway attempt.
            # Keep the persistent profile unchanged and retry only the launch.
            time.sleep(min(20.0, base_delay * attempt))
    raise RuntimeError("SparkBrowser failed to launch: %s" % last_error)


def _has_instagram_session(context) -> bool:
    try:
        cookies = context.cookies("https://www.instagram.com")
        return any(str(item.get("name") or "") == "sessionid" and item.get("value") for item in cookies)
    except Exception:
        return False


def _import_saved_cookies(context, account: str, proxy: str, mode: str) -> None:
    """Best-effort migration from old storage_state JSON into persistent context."""
    if _has_instagram_session(context):
        return
    candidates = [storage_state_path(account, proxy, mode), legacy_storage_state_path(account, mode)]
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cookies = data.get("cookies") if isinstance(data, dict) else None
            if cookies:
                context.add_cookies(cookies)
                if _has_instagram_session(context):
                    return
        except Exception:
            pass


def _browser_runtime_snapshot(page) -> Dict[str, Any]:
    try:
        return page.evaluate(
            """() => ({
              url: location.href,
              userAgent: navigator.userAgent,
              platform: navigator.platform,
              language: navigator.language,
              languages: navigator.languages,
              hardwareConcurrency: navigator.hardwareConcurrency,
              deviceMemory: navigator.deviceMemory || null,
              screen: {
                width: screen.width, height: screen.height,
                availWidth: screen.availWidth, availHeight: screen.availHeight,
                availLeft: screen.availLeft || 0, availTop: screen.availTop || 0,
                colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth
              },
              window: {
                innerWidth: innerWidth, innerHeight: innerHeight,
                outerWidth: outerWidth, outerHeight: outerHeight,
                screenX: screenX, screenY: screenY,
                devicePixelRatio: devicePixelRatio
              },
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
            })"""
        )
    except Exception as exc:
        return {"snapshot_error": str(exc)[:300]}


def _runtime_health(meta: Dict[str, Any], observed: Dict[str, Any], proxy: str) -> Dict[str, Any]:
    issues = []
    critical = []
    expected_os = str(meta.get("identity_os") or meta.get("identity_os_preference") or "")
    observed_os = _detect_identity_os({
        "navigator.platform": observed.get("platform"),
        "navigator.userAgent": observed.get("userAgent"),
    }, "")
    if expected_os and observed_os and expected_os != observed_os:
        critical.append("identity_os_mismatch")
    language = str(observed.get("language") or "")
    if language and not language.lower().startswith("en"):
        critical.append("browser_language_not_english")
    expected_geometry = meta.get("geometry") or {}
    expected_screen = expected_geometry.get("screen") or {}
    expected_viewport = expected_geometry.get("viewport") or {}
    screen = observed.get("screen") or {}
    window = observed.get("window") or {}
    if expected_screen and (
        abs(int(screen.get("width") or 0) - int(expected_screen.get("width") or 0)) > 4
        or abs(int(screen.get("height") or 0) - int(expected_screen.get("height") or 0)) > 4
    ):
        issues.append("screen_geometry_mismatch")
    if expected_viewport and (
        abs(int(window.get("innerWidth") or 0) - int(expected_viewport.get("width") or 0)) > 8
        or abs(int(window.get("innerHeight") or 0) - int(expected_viewport.get("height") or 0)) > 8
    ):
        issues.append("viewport_geometry_mismatch")
    if not str(observed.get("timezone") or "").strip():
        issues.append("timezone_not_observed")
    location = meta.get("runtime_location") or {}
    if proxy and location.get("source") != "proxy_geoip":
        critical.append("proxy_geoip_not_active")
    status = "broken" if critical else ("warning" if issues else "healthy")
    return {
        "status": status,
        "critical": critical,
        "warnings": issues,
        "expected_identity_os": expected_os,
        "observed_identity_os": observed_os,
        "location_source": location.get("source"),
    }


def _write_runtime_report(
    account: str,
    proxy: str,
    mode: str,
    meta: Dict[str, Any],
    used_launch: Dict[str, Any],
    page=None,
) -> None:
    safe_launch = {
        key: value for key, value in used_launch.items()
        if key not in {"proxy", "config", "fingerprint", "user_data_dir"}
    }
    payload: Dict[str, Any] = {
        "schema_version": 4,
        "account": account,
        "mode": mode,
        "profile_identity": meta.get("profile_identity"),
        "identity_os": meta.get("identity_os"),
        "geometry": meta.get("geometry"),
        "proxy_signature": proxy_signature(proxy),
        "proxy_present": bool(str(proxy or "").strip()),
        "location": meta.get("runtime_location"),
        "launch": safe_launch,
        "created_at": int(time.time()),
    }
    if page is not None:
        observed = _browser_runtime_snapshot(page)
        payload["observed"] = observed
        payload["health"] = _runtime_health(meta, observed, proxy)
    try:
        _atomic_write_json(_runtime_report_path(account, proxy, mode), payload)
    except Exception:
        pass


def open_spark_browser(
    account: str,
    proxy: str = "",
    mode: str = "desktop",
    headless: bool = False,
    locale: str = "",
    humanize: bool = True,
):
    """Open the persistent SparkBrowser profile. Returns (cm, context, page)."""
    try:
        from browser_brand import start_window_title_watcher
        start_window_title_watcher()
    except Exception:
        pass
    launch, meta = _build_launch_kwargs(account, proxy, mode, headless, locale=locale, humanize=humanize)
    user_data_dir = Path(str(launch["user_data_dir"]))
    _clear_session_restore(user_data_dir)
    _normalize_window_geometry(user_data_dir, meta["geometry"])
    manager, context, used_launch = _enter_camoufox(launch)
    _import_saved_cookies(context, account, proxy, mode)
    page = context.pages[0] if getattr(context, "pages", None) else context.new_page()

    # Track every page, but retain explicit operation-page ownership. New pages
    # are classified from fresh URL/DOM and never become primary merely because
    # they were created last.
    attach_page_router(context, page)
    _write_runtime_report(account, proxy, mode, meta, used_launch, page=page)
    return manager, context, page


def save_browser_state(context, account: str, proxy: str = "", mode: str = "desktop") -> str:
    try:
        path = storage_state_path(account, proxy, mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from instagram_session_snapshot import save_instagram_session
            save_instagram_session(context, path.parent, path)
        except Exception:
            tmp = path.with_suffix(path.suffix + ".tmp")
            context.storage_state(path=str(tmp))
            os.replace(str(tmp), str(path))
            try:
                os.chmod(str(path), 0o600)
            except Exception:
                pass
        return str(path)
    except Exception:
        return ""
