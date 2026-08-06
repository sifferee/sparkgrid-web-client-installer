#!/usr/bin/env python3
"""Instagram Reel publisher through the private Web API.

This module does not use instagrapi or the mobile API. It reuses the authenticated
SparkBrowser Web profile and reproduces the browser upload chain captured from
instagram.com:

    rupload_igvideo -> rupload_igphoto -> configure_to_clips

HTTP uploads can run in parallel. A maximum of two temporary SparkBrowser
sessions are used only when a saved Web session needs refreshing or when fresh
page tokens are required.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from publishing_history import create_history, mark_failed, mark_uploaded, update_history
from content_plans import next_plan_set, advance_plan, complete_plan_item, ensure_plan_schema
from platform_runtime import hidden_process_kwargs
from instagram_dialog_gate import continue_after_dialog
from browser_workflow_goal import (
    LOGIN_REQUIRED,
    SESSION_EXPORT_INCOMPLETE,
    SESSION_REFRESH_SUCCESS,
    STORAGE_PERSISTENCE_FAILED,
)
from instagram_session_goal import (
    StoragePersistenceOutcome,
    run_session_refresh_goal,
)
from instagram_session_snapshot import (
    SessionExportIncomplete,
    persist_instagram_session,
)

from instagram_web_upload import (
    SCALE_COOLDOWN_HOURS,
    SCALE_FIRST_CYCLE_POSTS,
    SCALE_STEADY_POSTS,
    LiveDump,
    create_job,
    db_conn,
    ensure_schema,
    mark_asset,
    now_iso,
    open_context,
    reserve_asset,
    selected_accounts,
    update_account,
    update_job,
)

try:
    from browser_launcher import (
        active_profile_dir,
        legacy_storage_state_path,
        parse_proxy_for_browser,
        storage_state_path,
    )
except Exception:  # pragma: no cover - project always ships browser_launcher
    active_profile_dir = None
    legacy_storage_state_path = None
    parse_proxy_for_browser = None
    storage_state_path = None


IG_APP_ID = "936619743392459"
IG_ASBD_ID = "129477"
CHUNK_SIZE = 10_000_000
BROWSER_REFRESH_LIMIT = 2
_BROWSER_REFRESH_SEMAPHORE = threading.BoundedSemaphore(BROWSER_REFRESH_LIMIT)


def log(message: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("browser", message, level)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip().lstrip("@"))[:90] or "account"


def normalise_accounts(raw: str) -> List[str]:
    return [part.strip().lstrip("@") for part in re.split(r"[,\n]+", str(raw or "")) if part.strip()]


def _account_proxy(account: dict, no_proxy: bool = False) -> str:
    if no_proxy:
        return ""
    return str(account.get("proxy") or account.get("proxy_url") or "").strip()


def _profile_root(account: str, proxy: str = "") -> Path:
    if active_profile_dir is not None:
        return Path(active_profile_dir(account, proxy, "desktop"))
    root = Path(os.environ.get("SPARKGRID_DATA_DIR") or Path(__file__).resolve().parent / "data")
    return root / "browser_profiles" / "ig_web_upload" / safe_name(account) / "desktop" / "profiles" / "default"


def _state_candidates(account: str, proxy: str = "") -> List[Path]:
    out: List[Path] = []
    if storage_state_path is not None:
        out.append(Path(storage_state_path(account, proxy, "desktop")))
    if legacy_storage_state_path is not None:
        out.append(Path(legacy_storage_state_path(account, "desktop")))
    out.append(_profile_root(account, proxy) / "camoufox_storage_state.json")
    seen: set[str] = set()
    result: List[Path] = []
    for path in out:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _load_storage_state(account: str, proxy: str = "") -> Optional[Dict[str, Any]]:
    for path in _state_candidates(account, proxy):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                return value
        except Exception:
            continue
    return None


def _session_snapshot(account: str, proxy: str = "") -> Dict[str, Any]:
    path = _profile_root(account, proxy) / "instagram_session.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _default_user_agent() -> str:
    import platform
    system = platform.system().lower()
    if system == "windows":
        prefix = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    elif system == "darwin":
        prefix = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    else:
        prefix = "Mozilla/5.0 (X11; Linux x86_64) "
    return prefix + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


def _user_agent(account: str, proxy: str = "") -> str:
    snap = _session_snapshot(account, proxy)
    value = str(snap.get("user_agent") or "").strip()
    if value:
        return value
    runtime = _profile_root(account, proxy) / "sparkbrowser_runtime.json"
    try:
        payload = json.loads(runtime.read_text(encoding="utf-8"))
        for key in ("user_agent", "userAgent"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
        value = str(browser.get("userAgent") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return _default_user_agent()


def _cookie_map_from_state(state: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in state.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name:
            result[name] = str(item.get("value") or "")
    return result


def _has_login_cookies(state: Optional[Dict[str, Any]]) -> bool:
    if not state:
        return False
    names = set(_cookie_map_from_state(state))
    return {"sessionid", "csrftoken", "ds_user_id"}.issubset(names)


def _tokens_from_storage_state(state: Optional[Dict[str, Any]]) -> Dict[str, str]:
    result = {"www_claim": "", "web_session_id": ""}
    if not isinstance(state, dict):
        return result
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict) or "instagram.com" not in str(origin.get("origin") or ""):
            continue
        values = {}
        for item in origin.get("localStorage") or []:
            if isinstance(item, dict) and item.get("name"):
                values[str(item.get("name"))] = str(item.get("value") or "")
        result["www_claim"] = values.get("www-claim-v2", values.get("www_claim", ""))
        result["web_session_id"] = values.get("web-session-id", values.get("web_session_id", ""))
        break
    return result


def _extract_page_tokens(html: str) -> Dict[str, str]:
    """Extract request tokens from the Instagram Web bootstrap HTML."""
    result = {
        "fb_dtsg": "",
        "jazoest": "",
        "instagram_ajax": "",
        "www_claim": "",
        "web_session_id": "",
    }
    text = str(html or "")

    # Current Instagram pages expose both fb_dtsg and jazoest in #__eqmc.
    match = re.search(r'<script[^>]+id=["\']__eqmc["\'][^>]*>(.*?)</script>', text, re.I | re.S)
    if match:
        try:
            payload = json.loads(match.group(1))
            if isinstance(payload, dict):
                result["fb_dtsg"] = str(payload.get("f") or "")
                query = parse_qs(urlparse(str(payload.get("u") or "")).query)
                result["jazoest"] = str((query.get("jazoest") or [""])[0])
        except Exception:
            pass

    if not result["fb_dtsg"]:
        patterns = [
            r'"DTSGInitialData"[^\n]{0,600}?"token"\s*:\s*"([^"]+)"',
            r'"DTSGInitData"[^\n]{0,600}?"token"\s*:\s*"([^"]+)"',
            r'"fb_dtsg"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            found = re.search(pattern, text, re.I)
            if found:
                result["fb_dtsg"] = found.group(1)
                break

    if not result["jazoest"]:
        found = re.search(r'[?&]jazoest=(\d+)', text)
        if found:
            result["jazoest"] = found.group(1)

    # data-btmanifest="1043089623_main"
    found = re.search(r'data-btmanifest=["\'](\d+)_', text, re.I)
    if found:
        result["instagram_ajax"] = found.group(1)
    else:
        found = re.search(r'"rollout_hash"\s*:\s*"([^"]+)"', text)
        if found:
            result["instagram_ajax"] = found.group(1)

    return result


def _tokens_from_page(page: Any) -> Dict[str, str]:
    try:
        value = page.evaluate(
            """() => {
              const out = {fb_dtsg:'', jazoest:'', instagram_ajax:'', www_claim:'', web_session_id:''};
              try {
                const eq = document.querySelector('#__eqmc');
                if (eq) {
                  const p = JSON.parse(eq.textContent || '{}');
                  out.fb_dtsg = String(p.f || '');
                  const u = new URL(String(p.u || ''), location.origin);
                  out.jazoest = String(u.searchParams.get('jazoest') || '');
                }
              } catch (_) {}
              try {
                const s = document.querySelector('script[data-btmanifest]');
                const raw = s ? String(s.dataset.btmanifest || '') : '';
                out.instagram_ajax = raw.split('_')[0] || '';
              } catch (_) {}
              try { out.www_claim = sessionStorage.getItem('www-claim-v2') || ''; } catch (_) {}
              try {
                out.web_session_id = sessionStorage.getItem('web-session-id') ||
                  localStorage.getItem('web-session-id') || '';
              } catch (_) {}
              return out;
            }"""
        )
        if isinstance(value, dict):
            return {key: str(value.get(key) or "") for key in (
                "fb_dtsg", "jazoest", "instagram_ajax", "www_claim", "web_session_id"
            )}
    except Exception:
        pass
    return {"fb_dtsg": "", "jazoest": "", "instagram_ajax": "", "www_claim": "", "web_session_id": ""}


def _request_proxy(proxy: str) -> Optional[Dict[str, str]]:
    if not proxy or parse_proxy_for_browser is None:
        return None
    try:
        parsed = parse_proxy_for_browser(proxy)
        return dict(parsed) if parsed else None
    except Exception:
        return None


def _request_context(playwright: Any, state: Dict[str, Any], user_agent: str, proxy: str = ""):
    kwargs: Dict[str, Any] = {
        "storage_state": state,
        "user_agent": user_agent,
        "ignore_https_errors": False,
        "timeout": 120_000,
        "extra_http_headers": {
            "accept-language": "en-US,en;q=0.9",
        },
    }
    proxy_cfg = _request_proxy(proxy)
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg
    return playwright.request.new_context(**kwargs)


def _bootstrap_via_http(playwright: Any, account: str, proxy: str) -> Optional[Tuple[Any, Dict[str, str]]]:
    state = _load_storage_state(account, proxy)
    if not _has_login_cookies(state):
        return None
    api = _request_context(playwright, state or {}, _user_agent(account, proxy), proxy)
    try:
        response = api.get(
            "https://www.instagram.com/?hl=en",
            headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=90_000,
            fail_on_status_code=False,
        )
        if response.status >= 400:
            api.dispose()
            return None
        html = response.text()
        if re.search(r'/accounts/login|log in to instagram', html, re.I):
            api.dispose()
            return None
        current_state = api.storage_state()
        if not _has_login_cookies(current_state):
            api.dispose()
            return None
        tokens = _extract_page_tokens(html)
        for key, value in _tokens_from_storage_state(current_state).items():
            if value:
                tokens[key] = value
        return api, tokens
    except Exception:
        try:
            api.dispose()
        except Exception:
            pass
        return None


def _refresh_via_browser(playwright: Any, account: dict, provider: str, proxy: str, headless: bool, dump: LiveDump) -> Tuple[Dict[str, Any], Dict[str, str], str]:
    """Open at most two browsers at once, refresh cookies and collect page tokens."""
    name = str(account.get("name") or "")
    with _BROWSER_REFRESH_SEMAPHORE:
        ctx_obj = context = page = None
        try:
            ctx_obj, context, page = open_context(
                playwright,
                name,
                mode="desktop",
                provider=provider,
                headless=headless,
                no_proxy=not bool(proxy),
            )
            try:
                page.goto("https://www.instagram.com/?hl=en", wait_until="domcontentloaded", timeout=90_000)
            except Exception:
                pass
            persisted: Dict[str, Any] = {}

            def persist_storage() -> StoragePersistenceOutcome:
                try:
                    state_path = (
                        Path(storage_state_path(name, proxy, "desktop"))
                        if storage_state_path is not None
                        else _profile_root(name, proxy)
                        / "camoufox_storage_state.json"
                    )
                    saved = persist_instagram_session(
                        context,
                        state_path.parent,
                        state_path,
                        account=name,
                    )
                    persisted.update(saved)
                    return StoragePersistenceOutcome(True)
                except SessionExportIncomplete:
                    return StoragePersistenceOutcome(
                        False,
                        category="session_export_incomplete",
                        export_complete=False,
                    )
                except Exception as exc:
                    return StoragePersistenceOutcome(
                        False, category=type(exc).__name__.lower()
                    )

            result, _callbacks = run_session_refresh_goal(
                page,
                workflow_run_id=str(getattr(dump, "run_id", "") or name),
                persist_storage=persist_storage,
                timeout_seconds=10.0,
                poll_interval=0.2,
            )
            if result.code is LOGIN_REQUIRED:
                raise RuntimeError("Web session refresh requires login")
            if result.code is SESSION_EXPORT_INCOMPLETE:
                raise RuntimeError("Web session export is incomplete")
            if result.code is STORAGE_PERSISTENCE_FAILED:
                raise RuntimeError(
                    "Web session storage persistence failed: "
                    + str(result.error_category or "validation_failed")
                )
            if result.code is not SESSION_REFRESH_SUCCESS:
                raise RuntimeError(
                    "Web session refresh did not reach its goal: "
                    + str(result.code.value)
                )
            storage = dict(persisted.get("state") or {})
            tokens = _tokens_from_page(page)
            ua = str(page.evaluate("() => navigator.userAgent") or _user_agent(name, proxy))
            dump.capture(page, "api_session_refreshed", "authenticated Web session ready", force_snapshot=True)
            return storage, tokens, ua
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if ctx_obj is not None and hasattr(ctx_obj, "__exit__"):
                    ctx_obj.__exit__(None, None, None)
            except Exception:
                pass


def _bootstrap_api(playwright: Any, account: dict, provider: str, no_proxy: bool, headless: bool, dump: LiveDump) -> Tuple[Any, Dict[str, str]]:
    """Bootstrap an API request context while Playwright is already active.

    This path is safe for the regular Playwright browser provider. Camoufox
    refreshes are handled by ``_api_session`` so Camoufox never starts its own
    synchronous Playwright runtime inside another sync_playwright context.
    """
    name = str(account.get("name") or "")
    proxy = _account_proxy(account, no_proxy=no_proxy)
    direct = _bootstrap_via_http(playwright, name, proxy)
    if direct is not None:
        api, tokens = direct
        log(f"{name}: using saved SparkBrowser Web session", "OK")
        return api, tokens

    log(f"{name}: saved Web session needs refresh; opening temporary SparkBrowser", "WARNING")
    state, tokens, ua = _refresh_via_browser(playwright, account, provider, proxy, headless, dump)
    for key, value in _tokens_from_storage_state(state).items():
        if value:
            tokens[key] = value
    api = _request_context(playwright, state, ua, proxy)
    response = api.get("https://www.instagram.com/?hl=en", timeout=90_000, fail_on_status_code=False)
    if response.status >= 400:
        api.dispose()
        raise RuntimeError(f"Instagram session refresh returned HTTP {response.status}")
    html_tokens = _extract_page_tokens(response.text())
    for key, value in html_tokens.items():
        if value:
            tokens[key] = value
    return api, tokens


@contextlib.contextmanager
def _api_session(account: dict, provider: str, no_proxy: bool, headless: bool, dump: LiveDump):
    """Yield ``(api_request_context, tokens)`` with safe runtime ordering.

    Camoufox owns an internal synchronous Playwright runtime. It cannot be
    launched while our separate sync_playwright context for HTTP requests is
    active in the same thread. For Camoufox we therefore:

      1. try the saved session inside a short HTTP Playwright context;
      2. close that context before opening SparkBrowser when refresh is needed;
      3. close SparkBrowser, then create a fresh HTTP Playwright context.

    The yielded request context always remains valid for the whole upload lane.
    """
    from playwright.sync_api import sync_playwright

    name = str(account.get("name") or "")
    proxy = _account_proxy(account, no_proxy=no_proxy)

    if provider != "camoufox":
        with sync_playwright() as playwright:
            api, tokens = _bootstrap_api(playwright, account, provider, no_proxy, headless, dump)
            try:
                yield api, tokens
            finally:
                try:
                    api.dispose()
                except Exception:
                    pass
        return

    # First attempt the persisted browser session. Yielding from inside this
    # context keeps the Playwright request runtime alive for the upload itself.
    with sync_playwright() as playwright:
        direct = _bootstrap_via_http(playwright, name, proxy)
        if direct is not None:
            api, tokens = direct
            log(f"{name}: using saved SparkBrowser Web session", "OK")
            try:
                yield api, tokens
            finally:
                try:
                    api.dispose()
                except Exception:
                    pass
            return

    # No Playwright runtime is active here. Camoufox can safely start and close
    # its own runtime before we create the HTTP request context below.
    log(f"{name}: saved Web session needs refresh; opening temporary SparkBrowser", "WARNING")
    state, tokens, ua = _refresh_via_browser(None, account, "camoufox", proxy, headless, dump)
    for key, value in _tokens_from_storage_state(state).items():
        if value:
            tokens[key] = value

    with sync_playwright() as playwright:
        api = _request_context(playwright, state, ua, proxy)
        try:
            response = api.get("https://www.instagram.com/?hl=en", timeout=90_000, fail_on_status_code=False)
            if response.status >= 400:
                raise RuntimeError(f"Instagram session refresh returned HTTP {response.status}")
            html_tokens = _extract_page_tokens(response.text())
            for key, value in html_tokens.items():
                if value:
                    tokens[key] = value
            yield api, tokens
        finally:
            try:
                api.dispose()
            except Exception:
                pass


def _cookie_map(api: Any) -> Dict[str, str]:
    try:
        state = api.storage_state()
    except Exception:
        state = {}
    return _cookie_map_from_state(state if isinstance(state, dict) else {})


def _headers(api: Any, tokens: Dict[str, str], *, referer: str) -> Dict[str, str]:
    cookies = _cookie_map(api)
    headers = {
        "accept": "*/*",
        "origin": "https://www.instagram.com",
        "referer": referer,
        "x-csrftoken": cookies.get("csrftoken", ""),
        "x-ig-app-id": IG_APP_ID,
        "x-asbd-id": IG_ASBD_ID,
        "x-requested-with": "XMLHttpRequest",
        "x-ig-max-touch-points": "0",
    }
    if cookies.get("mid"):
        headers["x-mid"] = cookies["mid"]
    if cookies.get("ig_did"):
        headers["x-ig-device-id"] = cookies["ig_did"]
    if tokens.get("www_claim"):
        headers["x-ig-www-claim"] = tokens["www_claim"]
    if tokens.get("instagram_ajax"):
        headers["x-instagram-ajax"] = tokens["instagram_ajax"]
    if tokens.get("web_session_id"):
        headers["x-web-session-id"] = tokens["web_session_id"]
    return headers


def _run_json(command: List[str]) -> Dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False, **hidden_process_kwargs())
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "command failed").strip()[-1200:])
    try:
        value = json.loads(completed.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"invalid JSON from {' '.join(command[:2])}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def _even(value: float) -> int:
    integer = max(2, int(round(value)))
    return integer if integer % 2 == 0 else integer - 1


def _bundled_ffmpeg() -> str:
    """Return system ffmpeg or the cross-platform imageio-ffmpeg binary."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        # On Windows, get_ffmpeg_exe() can call ``ver`` via platform.machine(),
        # which briefly flashes a console. The bundled executable has a stable
        # location, so resolve it directly without spawning any helper command.
        if os.name == "nt":
            roots = [Path(imageio_ffmpeg.__file__).resolve().parent / "binaries"]
            frozen_root = getattr(sys, "_MEIPASS", None)
            if frozen_root:
                roots.insert(0, Path(frozen_root) / "imageio_ffmpeg" / "binaries")
            for root in roots:
                for bundled in sorted(root.glob("ffmpeg*.exe")):
                    if bundled.is_file():
                        return str(bundled)
        candidate = str(imageio_ffmpeg.get_ffmpeg_exe() or "")
        if candidate and Path(candidate).is_file():
            return candidate
    except Exception:
        pass
    raise RuntimeError(
        "ffmpeg is required for API upload. Run install_windows.bat on Windows "
        "or ./install.command on macOS/Linux."
    )


def _probe_with_ffmpeg(video_path: str, ffmpeg: str) -> Dict[str, Any]:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True, timeout=45, check=False,
        **hidden_process_kwargs(),
    )
    text = (completed.stderr or "") + "\n" + (completed.stdout or "")
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video_lines = [line for line in text.splitlines() if "Video:" in line]
    dimension_match = None
    for line in video_lines:
        matches = re.findall(r"(?<!\d)(\d{2,5})x(\d{2,5})(?!\d)", line)
        if matches:
            dimension_match = matches[-1]
            break
    if not duration_match or not dimension_match:
        raise RuntimeError(f"could not read video dimensions/duration: {video_path}")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {"width": int(dimension_match[0]), "height": int(dimension_match[1]), "duration": duration}


def _probe_video(video_path: str) -> Dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        payload = _run_json([
            ffprobe,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "json",
            str(video_path),
        ])
        streams = payload.get("streams") or []
        stream = streams[0] if streams and isinstance(streams[0], dict) else {}
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
    else:
        basic = _probe_with_ffmpeg(video_path, _bundled_ffmpeg())
        width = int(basic["width"])
        height = int(basic["height"])
        duration = float(basic["duration"])
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError(f"could not read video dimensions/duration: {video_path}")

    # Upload the original video bytes without destructive preprocessing.
    # Instagram receives the full source frame as the crop rectangle.
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "duration_ms": max(1, int(round(duration * 1000))),
        "crop_width": width,
        "crop_height": height,
        "crop_x": 0,
        "crop_y": 0,
    }


def _make_cover(video_path: str, meta: Dict[str, Any], output_path: Path) -> bytes:
    ffmpeg = _bundled_ffmpeg()
    # Cover generation is the only FFmpeg use here. Preserve the whole
    # frame and letterbox it instead of center-cropping the customer's video.
    crop = (
        "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", "0.10", "-i", str(video_path),
        "-frames:v", "1", "-vf", crop,
        "-q:v", "2", str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False, **hidden_process_kwargs())
    if completed.returncode != 0 or not output_path.is_file():
        raise RuntimeError((completed.stderr or completed.stdout or "cover generation failed").strip()[-1200:])
    data = output_path.read_bytes()
    if not data:
        raise RuntimeError("generated cover is empty")
    return data


def _response_json(response: Any) -> Dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        try:
            return {"raw": response.text()[:2000]}
        except Exception:
            return {}


def _upload_video_chunks(api: Any, tokens: Dict[str, str], video_path: str, meta: Dict[str, Any], upload_id: str) -> Dict[str, Any]:
    path = Path(video_path)
    total = path.stat().st_size
    entity_name = f"fb_uploader_{upload_id}"
    url = f"https://i.instagram.com/rupload_igvideo/{entity_name}?hl=en"
    params = {
        "client-passthrough": "1",
        "is_clips_video": "1",
        "for_album": False,
        "is_sidecar": "0",
        "media_type": 2,
        "upload_id": upload_id,
        "upload_media_duration_ms": int(meta["duration_ms"]),
        "upload_media_height": int(meta["height"]),
        "upload_media_width": int(meta["width"]),
        "video_edit_params": {
            "crop_height": int(meta["crop_height"]),
            "crop_width": int(meta["crop_width"]),
            "crop_x1": int(meta["crop_x"]),
            "crop_y1": int(meta["crop_y"]),
            "mute": False,
            "trim_end": round(float(meta["duration"]), 6),
            "trim_start": 0,
        },
        "video_format": "",
        "video_transform": None,
    }
    base = _headers(api, tokens, referer="https://www.instagram.com/")
    base.update({
        "x-entity-name": entity_name,
        "x-entity-length": str(total),
        "x-instagram-rupload-params": json.dumps(params, separators=(",", ":")),
    })
    try:
        api.get(url, headers=base, timeout=60_000, fail_on_status_code=False)
    except Exception:
        pass

    offset = 0
    final_payload: Dict[str, Any] = {}
    with path.open("rb") as handle:
        while offset < total:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            headers = dict(base)
            headers["offset"] = str(offset)
            response = api.post(url, data=chunk, headers=headers, timeout=180_000, fail_on_status_code=False)
            payload = _response_json(response)
            expected_partial = offset + len(chunk) < total
            if expected_partial:
                if response.status not in {200, 206}:
                    raise RuntimeError(f"video chunk failed HTTP {response.status}: {payload}")
            else:
                if response.status != 200 or str(payload.get("status") or "").lower() != "ok":
                    raise RuntimeError(f"video final chunk failed HTTP {response.status}: {payload}")
                final_payload = payload
            offset += len(chunk)
    if offset != total:
        raise RuntimeError(f"video upload incomplete: {offset}/{total} bytes")
    return final_payload


def _upload_cover(api: Any, tokens: Dict[str, str], cover: bytes, meta: Dict[str, Any], upload_id: str) -> Dict[str, Any]:
    entity_name = f"fb_uploader_{upload_id}"
    url = f"https://i.instagram.com/rupload_igphoto/{entity_name}?hl=en"
    params = {
        "media_type": 2,
        "upload_id": upload_id,
        "upload_media_height": int(meta["height"]),
        "upload_media_width": int(meta["width"]),
    }
    headers = _headers(api, tokens, referer="https://www.instagram.com/")
    headers.update({
        "content-type": "image/jpeg",
        "offset": "0",
        "x-entity-length": str(len(cover)),
        "x-entity-name": entity_name,
        "x-entity-type": "image/jpeg",
        "x-instagram-rupload-params": json.dumps(params, separators=(",", ":")),
    })
    response = api.post(url, data=cover, headers=headers, timeout=120_000, fail_on_status_code=False)
    payload = _response_json(response)
    if response.status != 200 or str(payload.get("status") or "").lower() != "ok":
        raise RuntimeError(f"cover upload failed HTTP {response.status}: {payload}")
    return payload


def _configure_clip(api: Any, tokens: Dict[str, str], upload_id: str, caption: str) -> Dict[str, Any]:
    url = "https://www.instagram.com/api/v1/media/configure_to_clips/?hl=en"
    form = {
        "archive_only": "false",
        "caption": str(caption or ""),
        "clips_share_preview_to_feed": "1",
        "disable_comments": "0",
        "disable_oa_reuse": "false",
        "igtv_share_preview_to_feed": "1",
        "is_meta_only_post": "0",
        "is_unified_video": "1",
        "like_and_view_counts_disabled": "0",
        "media_share_flow": "creation_flow",
        "share_to_fb_destination_type": "USER",
        "source_type": "library",
        "upload_id": upload_id,
        "video_subtitles_enabled": "0",
    }
    if tokens.get("jazoest"):
        form["jazoest"] = tokens["jazoest"]
    if tokens.get("fb_dtsg"):
        form["fb_dtsg"] = tokens["fb_dtsg"]

    headers = _headers(api, tokens, referer="https://www.instagram.com/")
    last: Dict[str, Any] = {}
    for attempt in range(1, 31):
        response = api.post(url, form=form, headers=headers, timeout=120_000, fail_on_status_code=False)
        payload = _response_json(response)
        last = payload
        media = payload.get("media") if isinstance(payload.get("media"), dict) else None
        if response.status == 200 and media:
            media_id = str(media.get("id") or media.get("pk") or "")
            code = str(media.get("code") or "")
            return {
                "status": "ok",
                "media": media,
                "media_id": media_id,
                "code": code,
                "permalink": f"https://www.instagram.com/reel/{code}/" if code else "",
            }
        message = str(payload.get("message") or payload.get("error") or payload.get("raw") or "")
        if response.status in {200, 202} and re.search(r"transcode|processing|not finished", message, re.I):
            time.sleep(min(8.0, 1.5 + attempt * 0.35))
            continue
        raise RuntimeError(f"configure_to_clips failed HTTP {response.status}: {payload}")
    raise RuntimeError(f"configure_to_clips timed out: {last}")


def upload_reel_private_web_api(api: Any, tokens: Dict[str, str], video_path: str, caption: str = "", dump: Optional[LiveDump] = None) -> Dict[str, Any]:
    path = Path(video_path).expanduser()
    if not path.is_file():
        return {"ok": False, "step": "video", "error": f"video not found: {path}"}
    upload_id = str(int(time.time() * 1000))
    try:
        meta = _probe_video(str(path))
        if dump is not None:
            try:
                payload = {
                    "run_id": dump.run_id,
                    "account": dump.account,
                    "state": "api_video_prepared",
                    "action": json.dumps({"file": path.name, **meta}, ensure_ascii=False),
                    "error": "",
                    "url": "",
                    "ts": now_iso(),
                }
                with dump.actions_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception:
                pass
        with tempfile.TemporaryDirectory(prefix="sparkgrid_api_cover_") as td:
            cover = _make_cover(str(path), meta, Path(td) / "cover.jpg")
            _upload_video_chunks(api, tokens, str(path), meta, upload_id)
            _upload_cover(api, tokens, cover, meta, upload_id)
        configured = _configure_clip(api, tokens, upload_id, caption)
        return {"ok": True, "upload_id": upload_id, **configured}
    except Exception as exc:
        return {
            "ok": False,
            "upload_id": upload_id,
            "step": "private_web_api",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _tag_api_job(job_id: int) -> None:
    try:
        update_job(job_id, upload_engine="api", attempts=1)
    except Exception:
        pass


def _record_post_metadata(job_id: int, result: Dict[str, Any]) -> None:
    # Existing standalone DB versions do not yet have post_id/permalink columns.
    # Store them when a newer schema has those fields; otherwise current_step and
    # logs still contain the successful media id.
    try:
        conn = db_conn()
        try:
            cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(ig_web_upload_jobs)")}
            updates: Dict[str, Any] = {}
            if "post_id" in cols:
                updates["post_id"] = str(result.get("media_id") or "")
            if "permalink" in cols:
                updates["permalink"] = str(result.get("permalink") or "")
            if updates:
                update_job(job_id, **updates)
        finally:
            conn.close()
    except Exception:
        pass


def _asset_by_id(asset_id: int, account: str) -> Optional[Dict[str, Any]]:
    if not asset_id:
        return None
    conn = db_conn()
    try:
        row = conn.execute(
            """
            SELECT id,account_name,file_path,original_name,caption,status,content_kind
            FROM api_content_assets
            WHERE id=? AND (account_name='' OR account_name=?)
            """,
            (int(asset_id), account),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _new_or_existing_history(
    *, job_id: int, run_id: str, name: str, asset: Dict[str, Any], args: argparse.Namespace,
    caption: str, iteration: int,
) -> int:
    conn = db_conn()
    try:
        return create_history(
            conn,
            job_id=job_id,
            run_id=run_id,
            account_name=name,
            asset=asset,
            engine="api",
            provider=args.provider,
            background_web=True,
            caption=caption,
            history_id=int(args.history_id or 0) if iteration == 1 else 0,
        )
    finally:
        conn.close()


def account_lane(account: dict, args: argparse.Namespace, run_id: str) -> None:
    name = str(account.get("name") or "")
    content_mode = str(account.get("web_upload_content_mode") or "scale").lower()
    if content_mode not in {"scale", "quality"}:
        content_mode = "scale"
    forced_asset_id = int(getattr(args, "asset_id", 0) or 0)
    forced_retry = bool(forced_asset_id or int(getattr(args, "history_id", 0) or 0))
    dump = LiveDump(run_id, name)

    if content_mode == "scale" and not args.ignore_cooldown and not forced_retry:
        next_at = str(account.get("web_upload_next_cycle_at") or "").strip()
        if next_at:
            try:
                due = datetime.fromisoformat(next_at)
            except Exception:
                due = None
            if due and due > datetime.now():
                mins = (due - datetime.now()).total_seconds() / 60.0
                job_id = create_job(run_id, name, "private_web_api", "sparkbrowser_session", 1, str(dump.root))
                _tag_api_job(job_id)
                update_job(job_id, status="cooldown", current_step=f"scale resting {mins:.0f}m", last_error=f"next cycle {next_at}", finished_at=now_iso())
                log(f"{name}: scale cycle resting {mins:.0f}m", "WARNING")
                return

    cycle_count = int(account.get("web_upload_cycle_count") or 0)
    configured_set: Optional[Dict[str, Any]] = None
    run_assets: List[Dict[str, Any]] = []

    if forced_retry:
        asset = _asset_by_id(forced_asset_id, name)
        if asset:
            run_assets = [asset]
    elif content_mode == "quality":
        asset = reserve_asset(name, kind="quality")
        if asset:
            run_assets = [asset]
    else:
        conn = db_conn()
        try:
            ensure_plan_schema(conn)
            configured_set = next_plan_set(conn, name)
        finally:
            conn.close()
        if configured_set and configured_set.get("stopped"):
            job_id = create_job(run_id, name, "private_web_api", "sparkbrowser_session", 0, str(dump.root))
            _tag_api_job(job_id)
            message = "content plan completed — reset the plan position or add another set"
            update_job(job_id, status="no_content", current_step="content plan completed", last_error=message, finished_at=now_iso())
            update_account(name, web_upload_last_error=message)
            log(f"{name}: {message}", "WARNING")
            return
        if configured_set and configured_set.get("items"):
            run_assets = [dict(item) for item in configured_set["items"]]
        elif configured_set and configured_set.get("configured"):
            run_assets = []
        else:
            asset = reserve_asset(name, kind="scale")
            if asset:
                legacy_posts = SCALE_FIRST_CYCLE_POSTS if cycle_count == 0 else SCALE_STEADY_POSTS
                run_assets = [dict(asset) for _ in range(legacy_posts)]

    plan = len(run_assets)
    job_id = create_job(run_id, name, "private_web_api", "sparkbrowser_session", plan or 1, str(dump.root))
    _tag_api_job(job_id)
    posted = 0
    api = None
    active_history_id = 0
    plan_progress: Dict[str, Any] = {}

    if not run_assets:
        error = f"no ready {content_mode} content" if not forced_asset_id else f"content asset #{forced_asset_id} not found for {name}"
        update_job(job_id, status="no_content", current_step=error, last_error=error, finished_at=now_iso())
        update_account(name, web_upload_last_error=error)
        if int(getattr(args, "history_id", 0) or 0):
            conn = db_conn()
            try:
                mark_failed(conn, int(args.history_id), error)
            finally:
                conn.close()
        return

    for item in run_assets:
        if not item.get("file_path") or not Path(str(item["file_path"])).is_file():
            error = f"content file missing for asset #{int(item.get('asset_id') or item.get('id') or 0)}"
            update_job(job_id, status="failed", current_step="content file missing", last_error=error, finished_at=now_iso())
            update_account(name, web_upload_last_error=error)
            log(f"{name}: {error}", "ERROR")
            return
        if item.get("asset_id"):
            item["plan_item_id"] = item.get("id")
            item["id"] = int(item["asset_id"])

    try:
        first_asset = run_assets[0]
        first_caption = str(args.caption or first_asset.get("caption_override") or first_asset.get("caption") or "")
        active_history_id = _new_or_existing_history(
            job_id=job_id, run_id=run_id, name=name, asset=first_asset, args=args,
            caption=first_caption, iteration=1,
        )
        first_history_id = active_history_id
        set_title = str((configured_set or {}).get("title") or "")

        with _api_session(account, args.provider, args.no_proxy, args.session_headless, dump) as (api, tokens):
            for index, asset in enumerate(run_assets, start=1):
                caption = str(args.caption or asset.get("caption_override") or asset.get("caption") or "")
                active_history_id = first_history_id if index == 1 else _new_or_existing_history(
                    job_id=job_id, run_id=run_id, name=name, asset=asset, args=args,
                    caption=caption, iteration=index,
                )
                prefix = f"{set_title} · " if set_title else ""
                update_job(job_id, current_step=f"{prefix}API upload {index}/{plan}", posted_count=posted)
                log(f"{name}: {prefix}private Web API upload {index}/{plan} started", "OK")
                result = upload_reel_private_web_api(api, tokens, str(asset["file_path"]), caption, dump)
                if not result.get("ok"):
                    error = str(result.get("error") or result)
                    conn = db_conn()
                    try:
                        mark_failed(conn, active_history_id, error)
                    finally:
                        conn.close()
                    update_job(job_id, status="failed", current_step="private_web_api_failed", posted_count=posted, last_error=error, finished_at=now_iso())
                    update_account(name, web_upload_last_error=error)
                    log(f"{name}: {error}", "ERROR")
                    return
                posted += 1
                media_id = str(result.get("media_id") or "")
                code = str(result.get("code") or "")
                permalink = str(result.get("permalink") or "")
                conn = db_conn()
                try:
                    mark_uploaded(conn, active_history_id, media_id=media_id, shortcode=code, permalink=permalink, verifiable=True)
                    if configured_set and configured_set.get("strategy") == "custom" and asset.get("plan_item_id"):
                        plan_progress = complete_plan_item(conn, name, int(asset["plan_item_id"]))
                finally:
                    conn.close()
                step = f"{prefix}API posted {posted}/{plan}"
                if media_id:
                    step += f" · {media_id}"
                update_job(job_id, current_step=step, posted_count=posted)
                _record_post_metadata(job_id, result)
                update_account(name, web_upload_last_upload_at=now_iso(), web_upload_last_error="")
                log(f"{name}: Reel published through private Web API{(' · ' + permalink) if permalink else ''}", "OK")
                if index < plan:
                    time.sleep(random.uniform(args.between_min, args.between_max))

        if forced_retry:
            if content_mode == "quality":
                mark_asset(int(run_assets[0]["id"]), "uploaded", name)
            update_job(job_id, status="success", current_step="retry posted through API", posted_count=posted, finished_at=now_iso())
        elif content_mode == "quality":
            mark_asset(int(run_assets[0]["id"]), "uploaded", name)
            update_job(job_id, status="success", current_step="quality: 1 unique posted through API", posted_count=posted, finished_at=now_iso())
        else:
            if configured_set and configured_set.get("configured"):
                if configured_set.get("strategy") == "standard":
                    done_step = f"standard scale done ({posted})"
                else:
                    advanced = plan_progress or {
                        "current_set_order": int(configured_set.get("set_order") or 0),
                        "is_stopped": False,
                    }
                    plan_note = "plan stopped" if advanced.get("is_stopped") else f"next launch {int(advanced.get('current_set_order') or 0) + 1}"
                    done_step = f"{set_title or 'content pattern'} done ({posted}); {plan_note}"
            else:
                done_step = f"legacy scale cycle #{cycle_count + 1} done ({posted})"
            next_cycle = (datetime.now() + timedelta(hours=SCALE_COOLDOWN_HOURS)).isoformat(timespec="seconds")
            update_account(name, web_upload_cycle_count=cycle_count + 1, web_upload_next_cycle_at=next_cycle, web_upload_last_error="")
            update_job(job_id, status="success", current_step=f"{done_step}; next in {SCALE_COOLDOWN_HOURS:.0f}h", posted_count=posted, finished_at=now_iso())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if active_history_id:
            conn = db_conn()
            try:
                mark_failed(conn, active_history_id, error)
            finally:
                conn.close()
        elif int(getattr(args, "history_id", 0) or 0):
            conn = db_conn()
            try:
                mark_failed(conn, int(args.history_id), error)
            finally:
                conn.close()
        update_job(job_id, status="failed", current_step="api_worker_crashed", posted_count=posted, last_error=error, finished_at=now_iso())
        update_account(name, web_upload_last_error=error)
        log(f"{name}: {error}", "ERROR")
    finally:
        try:
            if api is not None:
                api.dispose()
        except Exception:
            pass

def main() -> int:
    parser = argparse.ArgumentParser(description="Instagram private Web API Reel uploader")
    parser.add_argument("--accounts", default="")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--provider", choices=["camoufox", "playwright"], default="camoufox")
    parser.add_argument("--caption", default="")
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--session-headless", action="store_true", default=True)
    parser.add_argument("--show-session-browser", action="store_true")
    parser.add_argument("--ignore-cooldown", action="store_true")
    parser.add_argument("--asset-id", type=int, default=0)
    parser.add_argument("--history-id", type=int, default=0)
    parser.add_argument("--between-min", type=float, default=2.0)
    parser.add_argument("--between-max", type=float, default=5.0)
    args = parser.parse_args()
    if args.show_session_browser:
        args.session_headless = False
    args.parallel = max(1, min(int(args.parallel or 1), 100))
    args.between_min = max(0.0, float(args.between_min))
    args.between_max = max(args.between_min, float(args.between_max))

    ensure_schema()
    accounts = selected_accounts(normalise_accounts(args.accounts))
    if not accounts:
        log("No accounts selected", "WARNING")
        return 2
    run_id = str(
        os.environ.get("SPARKGRID_RUN_ID")
        or datetime.now().strftime("api_run_%Y%m%d_%H%M%S")
    )
    workers = min(args.parallel, len(accounts))
    log(
        f"Private Web API engine: accounts={len(accounts)}, parallel={workers}, "
        f"browser_session_refresh_limit={BROWSER_REFRESH_LIMIT}",
        "OK",
    )
    if workers <= 1:
        for account in accounts:
            account_lane(account, args, run_id)
        return 0

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ig-private-api") as pool:
        futures = [pool.submit(account_lane, account, args, run_id) for account in accounts]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"API account worker crashed: {type(exc).__name__}: {exc}", "ERROR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
