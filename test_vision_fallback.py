"""One-off connectivity test for vision_fallback.py.

NOT part of the production automation — run this once by hand to confirm
ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / the model name all actually work
together, before wiring vision_fallback into any real account flow.

Deliberately does NOT touch Instagram or any real account — uses a plain
public page so a failure here can only mean "the API plumbing is wrong,"
never "something about a real account is wrong."
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vision_fallback import click_via_vision


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY не задан в окружении — подключи secrets.local.ps1 сначала.")
        return 1

    print(f"ANTHROPIC_BASE_URL = {os.environ.get('ANTHROPIC_BASE_URL') or '(не задан, будет официальный api.anthropic.com)'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.wikipedia.org/", wait_until="domcontentloaded", timeout=15000)

        result = click_via_vision(page, intent="the search input box on the page")

        print("\n--- РЕЗУЛЬТАТ ---")
        print(result)

        browser.close()

        if result.get("ok"):
            print("\nOK: ключ + адрес + модель работают вместе, координаты получены и клик прошёл.")
            return 0
        else:
            print(f"\nНЕ OK: reason={result.get('reason')}, detail={result.get('detail')}")
            print("Разбираем по reason:")
            print("  vision_unavailable -> проверь сам ключ/base_url/баланс на apiyi")
            print("  parse_failed       -> модель ответила не в ожидаемом формате, кинь 'detail' для разбора")
            print("  not_found          -> дошли до модели, но она не нашла элемент на простой странице — странно, кинь на разбор")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
