#!/usr/bin/env python3
"""Small cross-platform helpers used by SparkGrid subprocesses and launchers."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt" or sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"


def create_process_job(proc: subprocess.Popen[Any]) -> int | None:
    """Put a Windows process and all future descendants in a kill-on-close job."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(job)
            return None
        process_handle = wintypes.HANDLE(int(getattr(proc, "_handle", 0) or 0))
        if not process_handle or not kernel32.AssignProcessToJobObject(job, process_handle):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        return None


def close_process_job(handle: int | None) -> None:
    if not IS_WINDOWS or not handle:
        return
    try:
        import ctypes
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(int(handle))
    except Exception:
        pass


def terminate_process_job(handle: int | None) -> None:
    if not IS_WINDOWS or not handle:
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject(int(handle), 1)
    except Exception:
        pass
    finally:
        close_process_job(handle)


def process_group_kwargs() -> dict[str, Any]:
    """Create an isolated child process group on the current OS."""
    if IS_WINDOWS:
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def hidden_process_kwargs() -> dict[str, Any]:
    """Prevent short-lived console tools such as FFmpeg flashing a window."""
    if IS_WINDOWS:
        return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0))}
    return {}


def _taskkill(pid: int, force: bool = True) -> None:
    if not IS_WINDOWS:
        return
    command = ["taskkill", "/PID", str(int(pid)), "/T"]
    if force:
        command.append("/F")
    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    except Exception:
        pass


def stop_process_tree(proc: subprocess.Popen[Any], graceful_timeout: float = 5.0) -> None:
    """Stop a process and its descendants without using POSIX-only APIs on Windows."""
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        # Stop is an explicit emergency action. taskkill /T /F is used first
        # so Camoufox, Playwright, FFmpeg and lane workers cannot survive after
        # only their top-level scheduler process exits.
        _taskkill(proc.pid, force=True)
        try:
            proc.wait(timeout=max(3.0, graceful_timeout))
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=max(0.5, graceful_timeout))
        return
    except Exception:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def open_url(url: str) -> bool:
    try:
        return bool(webbrowser.open(str(url), new=2, autoraise=True))
    except Exception:
        return False


def downloads_dir() -> Path:
    candidate = Path.home() / "Downloads"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def platform_label() -> str:
    if IS_WINDOWS:
        return "Windows"
    if IS_MACOS:
        return "macOS"
    return "Linux"
