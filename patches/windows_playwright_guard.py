#!/usr/bin/env python3
"""Guard the Playwright Firefox driver against location-less page errors.

Camoufox/Firefox can emit Page.uncaughtError without a source location. The
Playwright 1.60 driver dereferences that optional value inside its Node process,
which terminates the whole browser session. This module applies the defensive
fallback used by the upstream fix proposal.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

OLD_LINES = (
    'url: pageError.location.url,',
    'line: pageError.location.lineNumber,',
    'column: pageError.location.columnNumber',
)
NEW_LINES = (
    'url: pageError.location?.url ?? "",',
    'line: pageError.location?.lineNumber ?? 0,',
    'column: pageError.location?.columnNumber ?? 0',
)


def locate_core_bundle() -> Path:
    import playwright  # type: ignore

    path = Path(playwright.__file__).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"
    if not path.is_file():
        raise FileNotFoundError(f"Playwright coreBundle.js not found: {path}")
    return path


def patch_file(path: Path) -> str:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    old_counts = [text.count(line) for line in OLD_LINES]
    new_counts = [text.count(line) for line in NEW_LINES]

    if all(count == 0 for count in old_counts):
        if all(count >= 1 for count in new_counts):
            return "already_patched"
        # A later Playwright/Camoufox build may contain the upstream fix in a
        # different form. Do not damage an unknown driver layout.
        return "not_required_or_unknown"

    # The three lines belong to the same payload and should appear equally.
    if not (old_counts[0] == old_counts[1] == old_counts[2]):
        raise RuntimeError(f"Unexpected Playwright driver layout: counts={old_counts}")

    backup = path.with_name(path.name + ".sparkgrid-before-pageerror-guard.bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    for old, new in zip(OLD_LINES, NEW_LINES):
        text = text.replace(old, new)

    temp = path.with_name(path.name + ".sparkgrid.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temp, path)

    verified = path.read_text(encoding="utf-8")
    if any(old in verified for old in OLD_LINES):
        raise RuntimeError("Playwright page-error guard verification failed")
    if not all(new in verified for new in NEW_LINES):
        raise RuntimeError("Playwright page-error fallback was not written")
    return f"patched_{old_counts[0]}_blocks"


def apply_guard(path: Optional[Path] = None, quiet: bool = False) -> str:
    target = Path(path) if path is not None else locate_core_bundle()
    result = patch_file(target)
    if not quiet:
        from log_config import get_logger
        lg = get_logger("playwright_guard")
        lg.info(f"Playwright page-error guard: {result}")
        lg.info(f"Driver: {target}")
        print(f"Playwright page-error guard: {result}")
        print(f"Driver: {target}")
    return result


if __name__ == "__main__":
    try:
        apply_guard()
    except Exception as exc:
        from log_config import get_logger
        get_logger("playwright_guard").error(f"Could not apply Playwright page-error guard: {exc}")
        print(f"ERROR: Could not apply Playwright page-error guard: {exc}")
        raise SystemExit(1)
