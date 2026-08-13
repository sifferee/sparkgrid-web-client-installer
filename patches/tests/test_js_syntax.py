"""Syntax-checks every embedded JS payload (`_XXX_SCRIPT = r\"\"\"...\"\"\"`) in
patches/*.py against a real JS engine (Node.js).

This exists because a broken JS payload (e.g. a duplicate `const` declaration,
or a variable used before its declaration) does NOT fail loudly: Playwright's
`frame.evaluate()` throws, that throw gets caught by a broad
`except Exception`, and the real error text is discarded in favor of a
generic reason string ("interaction_failed", "action_unavailable", ...).
A syntax error can silently masquerade as "the button wasn't found" for
weeks. This test catches that class of bug in under a second, before it
ever reaches a real account.

Requires Node.js to be installed and on PATH (`node --version`).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"

# Matches `SOME_NAME = r"""...` (module-level embedded JS payload constants).
SCRIPT_PATTERN = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*_SCRIPT)\s*=\s*r"""(.*?)"""', re.S | re.M)


def _discover_scripts() -> list[tuple[str, str, str]]:
    """Returns a list of (file_name, constant_name, js_source) tuples."""
    found = []
    for py_file in sorted(PATCHES_DIR.glob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        for match in SCRIPT_PATTERN.finditer(text):
            const_name, js_source = match.group(1), match.group(2)
            found.append((py_file.name, const_name, js_source))
    return found


_SCRIPTS = _discover_scripts()
_IDS = [f"{fname}::{const}" for fname, const, _ in _SCRIPTS]


@pytest.fixture(scope="session", autouse=True)
def _require_node():
    if shutil.which("node") is None:
        pytest.skip("node.js not found on PATH — cannot syntax-check embedded JS payloads")


@pytest.mark.skipif(not _SCRIPTS, reason="no embedded _SCRIPT constants found under patches/")
@pytest.mark.parametrize("file_name,const_name,js_source", _SCRIPTS, ids=_IDS)
def test_embedded_js_syntax(file_name: str, const_name: str, js_source: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(js_source)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    assert result.returncode == 0, (
        f"\n{file_name}: {const_name} failed to parse as valid JavaScript.\n"
        f"This means every frame.evaluate()/evaluate_handle() call using this "
        f"payload will throw and get swallowed by the caller's except-block.\n\n"
        f"node --check output:\n{result.stderr}"
    )


def test_discovered_at_least_one_script() -> None:
    """Sanity check: if this hits 0, the regex above stopped matching your
    naming convention (e.g. someone renamed a *_SCRIPT constant) and the
    parametrized test above is silently not testing anything."""
    assert _SCRIPTS, (
        "No `_SCRIPT = r\"\"\"...\"\"\"` constants found under patches/*.py — "
        "either there really are none right now, or the discovery regex in "
        "this test file no longer matches your naming convention."
    )
