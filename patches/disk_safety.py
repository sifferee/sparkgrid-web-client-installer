"""Bounded, privacy-safe disk protection for browser workflows.

All decisions are made for the actual target paths.  This module deliberately
does not expose local paths in results because results may reach the UI/logs.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RESERVE_BYTES = int(os.environ.get("SPARKGRID_DISK_RESERVE_BYTES", 2 * 1024**3))
DIAGNOSTIC_MAX_BYTES = int(os.environ.get("SPARKGRID_DIAGNOSTIC_MAX_BYTES", 1024**3))
DIAGNOSTIC_MAX_AGE_DAYS = int(os.environ.get("SPARKGRID_DIAGNOSTIC_MAX_AGE_DAYS", 14))
DIAGNOSTIC_MAX_RUNS = int(os.environ.get("SPARKGRID_DIAGNOSTIC_MAX_RUNS", 80))
_MAINTENANCE_LOCK = threading.Lock()
_VOLUME_PAUSE: dict[str, dict[str, Any]] = {}

def is_enospc(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) or "database or disk is full" in text or "sqlite_full" in text

def volume_key(path: Path) -> str:
    anchor = str(path.resolve().anchor or path.resolve().drive or "unknown").lower()
    return hashlib.sha256(anchor.encode()).hexdigest()[:12]

def disk_probe(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {"ok": True, "free_bytes": int(usage.free), "volume": volume_key(path)}
    except OSError:
        return {"ok": False, "code": "disk_space_probe_failed", "volume": volume_key(path)}

def preflight(paths: Iterable[Path], reserve_bytes: int = DEFAULT_RESERVE_BYTES) -> dict[str, Any]:
    reserve_bytes = max(1, int(reserve_bytes))
    checked: dict[str, dict[str, Any]] = {}
    for target in paths:
        probe = disk_probe(Path(target))
        key = str(probe["volume"])
        if key in checked:
            continue
        checked[key] = probe
        if not probe.get("ok"):
            _VOLUME_PAUSE[key] = {"code": "disk_space_probe_failed", "required_reserve_bytes": reserve_bytes}
            return {"ok": False, "code": "disk_space_probe_failed", "required_reserve_bytes": reserve_bytes, "volume": key}
        if int(probe["free_bytes"]) < reserve_bytes:
            _VOLUME_PAUSE[key] = {"code": "disk_space_low", "free_bytes": probe["free_bytes"], "required_reserve_bytes": reserve_bytes}
            return {"ok": False, "code": "disk_space_low", "free_bytes": probe["free_bytes"], "required_reserve_bytes": reserve_bytes, "volume": key}
        _VOLUME_PAUSE.pop(key, None)
    return {"ok": True, "checked_volumes": list(checked)}

def system_status() -> dict[str, Any]:
    return {"paused_volumes": list(_VOLUME_PAUSE.values())}

def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False

def retention(root: Path, *, active_runs: set[str] | None = None, max_bytes: int = DIAGNOSTIC_MAX_BYTES, max_age_days: int = DIAGNOSTIC_MAX_AGE_DAYS, max_runs: int = DIAGNOSTIC_MAX_RUNS) -> dict[str, int]:
    """Delete only completed run dirs below root; never follows links outside root."""
    stats = {"files_removed": 0, "bytes_reclaimed": 0, "errors": 0}
    active_runs = active_runs or set()
    if not root.exists() or not _MAINTENANCE_LOCK.acquire(blocking=False):
        return stats
    try:
        now = time.time(); entries = []
        for run in root.iterdir():
            if not run.is_dir() or run.is_symlink() or run.name in active_runs or not _inside(root, run):
                continue
            try:
                size = sum(p.stat().st_size for p in run.rglob("*") if p.is_file() and not p.is_symlink() and _inside(root, p))
                entries.append((run.stat().st_mtime, run, size))
            except OSError: stats["errors"] += 1
        total = sum(item[2] for item in entries)
        for mtime, run, size in sorted(entries):
            old = now - mtime > max_age_days * 86400
            excess = total > max_bytes or len(entries) > max_runs
            if not (old or excess): continue
            try:
                if _inside(root, run):
                    shutil.rmtree(run)
                    stats["files_removed"] += 1; stats["bytes_reclaimed"] += size; total -= size
            except OSError: stats["errors"] += 1
        return stats
    finally:
        _MAINTENANCE_LOCK.release()

@dataclass
class DiagnosticWriter:
    root: Path
    disabled: bool = False
    secondary_code: str = ""

    def write_text(self, target: Path, value: str) -> bool:
        if self.disabled: return False
        try:
            target.write_text(value, encoding="utf-8")
            return True
        except OSError as exc:
            if is_enospc(exc):
                self.disabled = True; self.secondary_code = "disk_space_exhausted"
            else: self.secondary_code = "diagnostic_write_failed"
            return False

    def append_text(self, target: Path, value: str) -> bool:
        if self.disabled: return False
        try:
            with target.open("a", encoding="utf-8") as handle: handle.write(value)
            return True
        except OSError as exc:
            if is_enospc(exc): self.disabled = True; self.secondary_code = "disk_space_exhausted"
            else: self.secondary_code = "diagnostic_write_failed"
            return False
