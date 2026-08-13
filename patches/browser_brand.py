#!/usr/bin/env python3
"""Best-effort SparkGrid branding of the Camoufox browser (dock/taskbar icon).

Called on desktop start via a background thread: it waits for Camoufox to be
installed (it is downloaded on first browser use), then swaps the browser icon
to the SparkGrid logo so clients see our brand instead of Camoufox/Firefox.

Everything is wrapped so it can NEVER crash the host app: any failure is a
silent no-op. Works from source (VPS) and from a PyInstaller bundle (assets are
resolved from sys._MEIPASS or deploy/browser_icon).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))


def _asset(name: str) -> Path | None:
    for base in (HERE, HERE / "deploy" / "browser_icon",
                 Path(os.path.dirname(os.path.abspath(__file__))) / "deploy" / "browser_icon"):
        p = base / name
        if p.exists():
            return p
    return None


def _install_dir() -> Path | None:
    try:
        from camoufox.pkgman import INSTALL_DIR  # type: ignore
        return Path(INSTALL_DIR)
    except Exception:
        try:
            from platformdirs import user_cache_dir  # type: ignore
            return Path(user_cache_dir("camoufox"))
        except Exception:
            return None


def apply_once() -> bool:
    """Try to brand the installed Camoufox. Returns True once done (or already
    branded), False if Camoufox isn't installed yet (so the caller can retry)."""
    try:
        base = _install_dir()
        if not base or not base.exists():
            return False
        if sys.platform == "darwin":
            icns = _asset("SparkGridBrowser.icns") or _asset("SparkGrid.icns")
            if not icns:
                return True  # nothing to do; don't spin forever
            app = base / "Camoufox.app"
            if not app.is_dir():
                return False  # not installed yet
            pl_path = app / "Contents" / "Info.plist"
            icon_name = "firefox.icns"
            try:
                import plistlib
                with open(pl_path, "rb") as f:
                    pl = plistlib.load(f)
                icon_name = str(pl.get("CFBundleIconFile") or icon_name)
                if not icon_name.endswith(".icns"):
                    icon_name += ".icns"
            except Exception:
                pl = {}
            icon = app / "Contents" / "Resources" / icon_name
            # already ours? (size match) -> skip
            if icon.exists() and icon.stat().st_size == icns.stat().st_size:
                return True
            backup = icon.with_name(icon.name + ".orig")
            if icon.exists() and not backup.exists():
                shutil.copy2(icon, backup)
            shutil.copy2(icns, icon)
            try:
                import plistlib
                pl["CFBundleName"] = "SparkBrowser"
                pl["CFBundleDisplayName"] = "SparkBrowser"
                pl["CFBundleIconFile"] = icon_name
                with open(pl_path, "wb") as f:
                    plistlib.dump(pl, f)
            except Exception:
                pass
            os.utime(app, None)
            try:
                os.utime(icon, None)
                os.utime(pl_path, None)
            except Exception:
                pass
            subprocess.run(
                ["/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
                 "LaunchServices.framework/Versions/A/Support/lsregister", "-f", str(app)],
                capture_output=True)
            # macOS caches Dock icons aggressively. Best-effort local refresh:
            # harmless if it fails, and the icon still updates after a reboot.
            try:
                cache_dir = Path.home() / "Library" / "Application Support" / "com.apple.sharedfilelist" / "com.apple.LSSharedFileList.ApplicationRecentDocuments"
                for cached in cache_dir.glob("*camoufox*.sfl*"):
                    cached.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        if sys.platform.startswith("win"):
            ico = _asset("SparkGridBrowser.ico") or _asset("SparkGrid.ico")
            exe = base / "camoufox.exe"
            if not ico or not exe.exists():
                return exe.exists()  # if exe missing, retry; else give up
            rc = _asset("rcedit-x64.exe") or (HERE / "rcedit-x64.exe")
            if not Path(rc).exists():
                try:
                    import urllib.request
                    urllib.request.urlretrieve(
                        "https://github.com/electron/rcedit/releases/latest/download/rcedit-x64.exe",
                        str(HERE / "rcedit-x64.exe"))
                    rc = HERE / "rcedit-x64.exe"
                except Exception:
                    return True  # can't fetch tool; stop retrying
            commands = [
                [str(rc), str(exe), "--set-icon", str(ico)],
                [str(rc), str(exe), "--set-version-string", "ProductName", "SparkBrowser"],
                [str(rc), str(exe), "--set-version-string", "FileDescription", "SparkBrowser"],
                [str(rc), str(exe), "--set-version-string", "InternalName", "SparkBrowser"],
            ]
            ok = True
            for command in commands:
                result = subprocess.run(command, capture_output=True)
                ok = ok and result.returncode == 0
            try:
                subprocess.run(["ie4uinit.exe", "-show"], capture_output=True)
            except Exception:
                pass
            return True  # branding is best-effort and must never loop forever
        return True  # linux/headless: nothing to do
    except Exception:
        return True  # never crash the host; stop retrying



# Keep the visible Windows product identity separate from the internal engine
# package name.  The executable resources are branded during staging; this
# watcher handles the live native title, which Gecko refreshes after tab/title
# changes.  Only windows owned by the bundled SparkBrowser executable qualify.
_WINDOW_BRAND_WATCHER_STARTED = False


def _sparkbrowser_executable() -> Path | None:
    base = _install_dir()
    if not base:
        return None
    try:
        return (base / "camoufox.exe").resolve()
    except Exception:
        return base / "camoufox.exe"


def _windows_process_path(pid: int) -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    except Exception:
        return None


def _brand_open_windows_once() -> int:
    if not sys.platform.startswith("win"):
        return 0
    expected = _sparkbrowser_executable()
    if not expected or not expected.is_file():
        return 0
    expected_norm = os.path.normcase(os.path.abspath(str(expected)))
    changed = 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd, _lparam):
            nonlocal changed
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                process_path = _windows_process_path(int(pid.value))
                if not process_path:
                    return True
                actual_norm = os.path.normcase(os.path.abspath(str(process_path)))
                if actual_norm != expected_norm:
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value
                branded = title.replace("Camoufox", "SparkBrowser")
                if branded != title:
                    user32.SetWindowTextW(hwnd, branded)
                    changed += 1
            except Exception:
                pass
            return True

        user32.EnumWindows(visit, 0)
    except Exception:
        return changed
    return changed


def _watch_window_titles(timeout_s: int = 12 * 60 * 60, interval_s: float = 0.4) -> None:
    deadline = time.time() + max(10, int(timeout_s))
    while time.time() < deadline:
        _brand_open_windows_once()
        time.sleep(max(0.2, float(interval_s)))


def start_window_title_watcher() -> None:
    global _WINDOW_BRAND_WATCHER_STARTED
    if _WINDOW_BRAND_WATCHER_STARTED or not sys.platform.startswith("win"):
        return
    _WINDOW_BRAND_WATCHER_STARTED = True
    try:
        import threading
        threading.Thread(
            target=_watch_window_titles,
            name="sparkbrowser-window-brand",
            daemon=True,
        ).start()
    except Exception:
        _WINDOW_BRAND_WATCHER_STARTED = False


def watch_and_brand(timeout_s: int = 1800, interval_s: int = 20) -> None:
    """Background loop: retry apply_once until Camoufox appears (or timeout)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if apply_once():
                return
        except Exception:
            return
        time.sleep(interval_s)


def start_background() -> None:
    """Fire-and-forget daemon thread. Safe to call from app startup."""
    try:
        import threading
        threading.Thread(target=watch_and_brand, daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    print("branded" if apply_once() else "camoufox not installed yet")
