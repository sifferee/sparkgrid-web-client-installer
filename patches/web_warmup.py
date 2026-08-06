#!/usr/bin/env python3
"""
web_warmup.py — Standalone general browser activity warmup (Camoufox).

PURPOSE
    Build a stable, persistent general-browsing profile with ordinary activity:
    types varied search queries, opens organic results, *reads* articles
    (adaptive dwell + chunked scrolling + re-reads), browses Google Shopping
    products, moves the mouse, uses one page in one window and follows links in-place.

    WEB ONLY. This module never touches Instagram and never logs into anything.
    It is a pure "warm the fingerprint" pass you run *before* any account work.

ARCHITECTURE
    Reactive DOM rule engine — mirrors the phone-side bot:
      observe(page) -> PageState   (single source of truth, re-read every tick)
      RULES (ordered): hard reactions first (consent / captcha / blocked)
      Director: when no hard rule fires, pick a weighted human INTENT
    Nothing is blindly scripted: every step re-reads the live page and reacts.

    Provider is Camoufox (humanize + geoip + fingerprint spoofing). No AdsPower,
    no AI content, no DB dependency required.

CAPTCHA / BOT-CHECK
    Detected, never solved. On detection the engine backs off, leaves for a
    neutral site, and rotates to a different search engine. Cookie/consent
    gates are ACCEPTED (build a realistic returning-visitor cookie jar).

DRIVER CRASHES / HANGS (important)
    Camoufox is pinned to a specific Playwright (Juggler protocol). A mismatched
    Playwright makes the Firefox driver crash ("Cannot read properties of
    undefined (reading 'url')") or hang the first newPage/goto forever.
    KNOWN-GOOD: camoufox==0.4.11  WITH  playwright==1.53.0.
        pip install -U "camoufox[geoip]==0.4.11" "playwright==1.53.0"
        python3 -m camoufox fetch
    Each warmup runs as a CHILD subprocess under a SUPERVISOR that relaunches on
    crash AND on hang (it watches the child's heartbeat = latest_state.json
    mtime; no progress for ~75s with the browser up => kill & resume).
    Python 3.11/3.12 recommended (system 3.9 + very new Node can be unstable).

USAGE
    # single profile
    python3 web_warmup.py --minutes 8 --profile testprofile1 --persona shopper
    python3 web_warmup.py --minutes 12 --profile acc1 --proxy host:port:user:pass

    # BATCH (many profiles, bounded concurrency — warms each profile separately)
    python3 web_warmup.py --minutes 8 --parallel 4 --profiles "acc1,acc2,acc3"
    python3 web_warmup.py --minutes 8 --parallel 5 --profiles-file profiles.txt --persona random
    python3 web_warmup.py --minutes 8 --parallel 4 --from-db --headless
      # profiles.txt: one per line, "profile" or "profile|host:port:user:pass"
      # --from-db pulls name+proxy from bot.db accounts (enabled=1)
      # progress -> browser_warmup_data/batch/<id>/status.json (+ per-profile .log)

    python3 web_warmup.py --dry-run            # offline self-test, no browser

    FastAPI should launch this via subprocess (per project rule: server never
    imports automation modules directly).
"""

from __future__ import annotations

from instagram_dialog_gate import continue_after_dialog

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from platform_runtime import process_group_kwargs, stop_process_tree
from typing import Callable, Dict, List, Optional, Tuple
from disk_safety import DiagnosticWriter

try:
    from browser_launcher import (
        open_spark_browser,
        save_browser_state,
        storage_state_path as sparkbrowser_state_path,
    )
except Exception:
    open_spark_browser = None
    save_browser_state = None
    sparkbrowser_state_path = None

try:
    from browser_preferences import load_browser_preferences, save_browser_preferences
except Exception:
    load_browser_preferences = None
    save_browser_preferences = None

try:
    from ig_human import make_human
except Exception:
    make_human = None

ROOT = Path(__file__).resolve().parent
# In the desktop app, this script is bundled read-only inside the app/exe.
# Runtime state must live in the same writable data dir as the rest of SparkGrid.
DATA_ROOT = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT)
DEBUG_ROOT = DATA_ROOT / "browser_warmup_data" / "debug" / "web_warmup"
PROFILE_ROOT = Path(os.environ.get("WEB_WARMUP_PROFILE_ROOT") or (DATA_ROOT / "browser_profiles" / "web_warmup"))

RESET = "\033[0m"
COLORS = {"OK": "\033[92m", "ERROR": "\033[91m", "WARNING": "\033[93m", "INFO": "\033[94m", "ACT": "\033[96m"}


def log(msg: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("warmup", msg, level)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:90] or "profile"


class BrowserDead(Exception):
    """Raised when the Playwright/Firefox driver process dies mid-session
    (a known Playwright FF driver crash). The supervisor relaunches and resumes."""


_DEAD_SIGNALS = (
    "target page, context or browser has been closed", "target closed",
    "browser has been closed", "browser closed", "context or browser has been closed",
    "connection closed", "page crashed", "page closed", "has been closed",
    "websocket", "pipe", "epipe", "browser.new_context", "connection to the browser",
)


def _is_dead_error(exc: Exception) -> bool:
    return any(s in str(exc).lower() for s in _DEAD_SIGNALS)


# =====================================================================
#  Personas — coherent interest profiles so queries look like a real
#  person with a life, not a random keyword spammer.
# =====================================================================

PERSONAS: Dict[str, Dict] = {
    "generalist": {
        "topics": [
            ("how to {verb} {thing}", {"verb": ["clean", "fix", "store", "cook", "organize"],
                                       "thing": ["cast iron pan", "leather shoes", "sourdough starter",
                                                 "a leaking tap", "old photos"]}),
            ("{place} weather forecast", {"place": ["vienna", "berlin", "lisbon", "prague", "milan"]}),
            ("best {item} 2026 reviews", {"item": ["wireless earbuds", "robot vacuum", "espresso machine",
                                                   "office chair", "e reader"]}),
            ("{topic} explained simply", {"topic": ["compound interest", "the krebs cycle", "tariffs",
                                                    "how vaccines work", "el nino"]}),
        ],
        "news": ["technology news today", "europe travel news", "science discovery 2026"],
        "shop": ["mechanical keyboard", "running shoes men", "stainless water bottle", "desk lamp led"],
    },
    "shopper": {
        "topics": [
            ("{brand} {item} review", {"brand": ["sony", "samsung", "anker", "logitech", "bosch"],
                                       "item": ["headphones", "monitor", "power bank", "mouse", "blender"]}),
            ("{item} vs {item2} comparison", {"item": ["iphone 16", "kindle", "airpods pro"],
                                              "item2": ["pixel 9", "kobo clara", "galaxy buds"]}),
            ("cheapest {item} with free shipping", {"item": ["laptop stand", "usb c cable", "yoga mat"]}),
        ],
        "news": ["best tech deals this week", "gadget releases 2026"],
        "shop": ["noise cancelling headphones", "ergonomic office chair", "4k monitor 27 inch",
                 "air fryer", "standing desk", "running shoes women", "mechanical watch"],
    },
    "foodie": {
        "topics": [
            ("authentic {dish} recipe", {"dish": ["carbonara", "ramen", "biryani", "shakshuka", "pho"]}),
            ("how long to {verb} {food}", {"verb": ["boil", "roast", "marinate", "proof"],
                                           "food": ["eggs", "chicken thighs", "pizza dough", "beef brisket"]}),
            ("best restaurants in {city}", {"city": ["vienna", "rome", "tokyo", "paris", "lyon"]}),
        ],
        "news": ["food trends 2026", "michelin guide news"],
        "shop": ["chef knife", "cast iron skillet", "stand mixer", "coffee grinder", "dutch oven"],
    },
    "techie": {
        "topics": [
            ("{lang} {concept} tutorial", {"lang": ["python", "rust", "typescript", "go"],
                                           "concept": ["async await", "decorators", "generics", "channels"]}),
            ("how to {task} in {tool}", {"task": ["rebase", "mock a request", "profile memory"],
                                         "tool": ["git", "pytest", "docker"]}),
            ("{topic} best practices", {"topic": ["rest api design", "database indexing", "logging", "caching"]}),
        ],
        "news": ["open source news 2026", "ai research this week", "linux kernel news"],
        "shop": ["mechanical keyboard", "ultrawide monitor", "raspberry pi 5", "usb hub"],
    },
}

# Content sources that load reliably for "reading" behaviour (no login walls).
NEUTRAL_READS = [
    "https://www.wikihow.com/Main-Page",
    "https://text.npr.org/",
    "https://lite.cnn.com/",
    "https://en.wikipedia.org/wiki/Special:Random",
]

# Real websites the engine visits directly and then *walks around inside*.
# Chosen to be LIGHTWEIGHT / low-JS so they don't trip the Firefox driver's
# uncaught-pageError crash. Heavy SPA news sites (Verge/CNET/Ars) are avoided
# on purpose — text mirrors (lite.cnn.com, text.npr.org) are used instead.
CONTENT_SITES: Dict[str, List[str]] = {
    "generalist": ["https://lite.cnn.com/", "https://text.npr.org/",
                   "https://www.bbc.com/news", "https://www.wikihow.com/Main-Page",
                   "https://en.wikipedia.org/wiki/Special:Random"],
    "shopper": ["https://lite.cnn.com/", "https://www.bbc.com/news/technology",
                "https://text.npr.org/", "https://www.wikihow.com/Main-Page",
                "https://en.wikipedia.org/wiki/Special:Random"],
    "foodie": ["https://www.allrecipes.com/", "https://www.bbcgoodfood.com/",
               "https://www.wikihow.com/Main-Page", "https://text.npr.org/",
               "https://en.wikipedia.org/wiki/Special:Random"],
    "techie": ["https://news.ycombinator.com/", "https://lite.cnn.com/",
               "https://text.npr.org/", "https://www.bbc.com/news/technology",
               "https://en.wikipedia.org/wiki/Special:Random"],
}

# Search engines. Runtime v2.3 pins one preferred engine per profile. Google
# is the default; Bing and DuckDuckGo are only session fallbacks when the
# preferred engine is unavailable or shows a consent/captcha wall.
SEARCH_ENGINES = {
    "google": {
        "search_url": "https://www.google.com/search?q={q}",
        "result_sel": "a:has(h3), div#search a h3",
        "weight": 10,
    },
    "bing": {
        "search_url": "https://www.bing.com/search?q={q}",
        "result_sel": "li.b_algo h2 a, h2 a",
        "weight": 2,
    },
    "duckduckgo": {
        "search_url": "https://duckduckgo.com/?q={q}",
        "result_sel": "a[data-testid='result-title-a'], a.result__a, h2 a",
        "weight": 1,
    },
}

GOOGLE_SHOPPING = "https://www.google.com/search?tbm=shop&q={q}"

# Words that mark a page as a cookie/consent gate (independent of which
# button we click — used only for classification).
CONSENT_PAGE_SIGNALS = ["cookie", "consent", "gdpr", "we use cookies",
                        "before you continue", "datenschutz", "согласие на использование"]
# Buttons we click — we ACCEPT cookies on warmup to build a realistic cookie
# jar / returning-visitor trust. Ordered specific-first to avoid mis-clicks.
ACCEPT_LABELS = [
    "accept all", "accept all cookies", "allow all", "agree to all", "i agree",
    "accept", "agree", "got it", "alle akzeptieren", "akzeptieren", "zustimmen",
    "принять все", "принять", "согласиться", "хорошо",
]
CAPTCHA_SIGNALS = [
    "unusual traffic", "are you a robot", "verify you are human", "i'm not a robot",
    "recaptcha", "/sorry/", "captcha", "detected unusual", "automated queries",
    "необычный трафик", "подтвердите, что вы не робот",
]


# =====================================================================
#  WarmupDump — live debug artifacts, same shape as the IG web engine's
#  LiveDump so the dashboard can read them later. Degrades gracefully
#  when no real page (dry-run) is present.
# =====================================================================

class WarmupDump:
    def __init__(self, run_id: str, profile: str, max_snapshots: int = 30,
                 screenshots: bool = False):
        self.run_id = run_id
        self.profile = safe_name(profile)
        self.root = DEBUG_ROOT / run_id / self.profile
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_snapshots = int(max_snapshots or 30)
        self.screenshots = bool(screenshots)
        self.last_state = ""
        self.actions_file = self.root / "actions.jsonl"
        self.writer = DiagnosticWriter(self.root)
        DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
        (DEBUG_ROOT / "latest_run.txt").write_text(run_id, encoding="utf-8")
        (DEBUG_ROOT / "latest_profile.txt").write_text(self.profile, encoding="utf-8")
        self._heartbeat("worker_ready")

    def _heartbeat(self, state: str) -> None:
        target = str(os.environ.get("SPARKGRID_HEARTBEAT_PATH") or "").strip()
        if not target:
            return
        try:
            Path(target).write_text(f"{now_iso()} {state}\n", encoding="utf-8")
        except OSError:
            pass

    def capture(self, page, state: str, action: str = "", error: str = "",
                extra: Optional[dict] = None, force_snapshot: bool = False) -> None:
        self._heartbeat(state)
        if self.writer.disabled:
            return
        payload = {"run_id": self.run_id, "profile": self.profile, "state": state,
                   "action": action, "error": error, "url": "", "ts": now_iso()}
        if extra:
            payload.update(extra)
        try:
            payload["url"] = getattr(page, "url", "") or ""
        except Exception:
            pass

        # Heartbeat MUST be written before any optional browser-side diagnostic.
        # In older builds page.screenshot() ran first; if Firefox stalled there,
        # the supervisor saw no progress and killed a healthy session after 75s.
        if not self.writer.write_text(self.root / "latest_state.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
        if not self.writer.append_text(self.actions_file, json.dumps(payload, ensure_ascii=False) + "\n"): return

        want_snapshot = bool(self.screenshots and page is not None and hasattr(page, "screenshot")
                             and (force_snapshot or error or state != self.last_state))
        if want_snapshot:
            try:
                page.screenshot(path=str(self.root / "latest.png"), full_page=False, timeout=5000)
            except Exception as exc:
                payload["screenshot_error"] = str(exc)[:200]
                self.writer.write_text(self.root / "latest_state.json", json.dumps(payload, ensure_ascii=False, indent=2))

        if force_snapshot or error or state != self.last_state:
            self.last_state = state
            stamp = datetime.now().strftime("%H%M%S")
            snap_dir = self.root / "snapshots"
            snap_dir.mkdir(exist_ok=True)
            base = f"{stamp}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', state)[:45]}"
            try:
                if want_snapshot and (self.root / "latest.png").exists():
                    shutil.copy2(self.root / "latest.png", snap_dir / f"{base}.png")
            except Exception:
                pass
            if not self.writer.write_text(snap_dir / f"{base}.json", json.dumps(payload, ensure_ascii=False, indent=2)): return
            snaps = sorted([p for p in snap_dir.iterdir() if p.is_file()],
                           key=lambda p: p.stat().st_mtime)
            overflow = max(0, len(snaps) - self.max_snapshots * 2)
            for p in snaps[:overflow]:
                try:
                    p.unlink()
                except Exception:
                    pass


# =====================================================================
#  PageState — the single source of truth, re-read every tick.
# =====================================================================

@dataclass
class PageState:
    url: str = ""
    host: str = ""
    title: str = ""
    text_len: int = 0
    word_count: int = 0
    kind: str = "unknown"        # consent | captcha | blocked | serp | shopping | article | home | error
    has_results: bool = False
    n_links: int = 0
    scroll_y: int = 0
    scroll_h: int = 0
    at_bottom: bool = False


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", str(url or ""))
    return (m.group(1) if m else "").lower()


def observe(page, engine_hint: str = "") -> PageState:
    """Read live ground truth from the page and classify it. Never raises."""
    st = PageState()
    try:
        st.url = page.url or ""
    except Exception:
        pass
    st.host = _host_of(st.url)
    try:
        st.title = (page.title() or "")[:160]
    except Exception:
        pass

    body_text = ""
    try:
        body_text = (page.locator("body").inner_text(timeout=1500) or "")
    except Exception:
        body_text = ""
    st.text_len = len(body_text)
    st.word_count = len(body_text.split())

    low = (body_text[:6000] + " " + st.url + " " + st.title).lower()

    # scroll metrics
    try:
        m = page.evaluate(
            "() => ({y: window.scrollY, h: document.body ? document.body.scrollHeight : 0,"
            " vh: window.innerHeight, n: document.querySelectorAll('a').length})")
        st.scroll_y = int(m.get("y", 0))
        st.scroll_h = int(m.get("h", 0))
        st.n_links = int(m.get("n", 0))
        st.at_bottom = (st.scroll_y + int(m.get("vh", 0)) + 80) >= st.scroll_h if st.scroll_h else False
    except Exception:
        pass

    # classify — hard signals first
    if any(s in low for s in CAPTCHA_SIGNALS):
        st.kind = "captcha"
        return st
    if any(s in low for s in CONSENT_PAGE_SIGNALS) and any(a in low for a in ACCEPT_LABELS):
        st.kind = "consent"
        return st
    if "shop" in st.url and ("tbm=shop" in st.url or "/shopping" in st.url):
        st.kind = "shopping"
    elif st.host in ("www.google.com", "www.bing.com", "duckduckgo.com") and (
            "/search" in st.url or "?q=" in st.url or st.url.endswith("/")):
        st.kind = "serp"
        st.has_results = st.n_links > 8
    elif "wikipedia.org" in st.host or "wikihow.com" in st.host or st.word_count > 250:
        st.kind = "article"
    elif st.host:
        st.kind = "home"
    else:
        st.kind = "error"
    st.has_results = st.has_results or (st.kind == "serp" and st.n_links > 8)
    return st


# =====================================================================
#  Human action primitives — small, reactive, never raise.
# =====================================================================

def _sleep(a: float, b: float) -> None:
    time.sleep(random.uniform(float(a), float(b)))


def human_mouse(page, moves: int = 0, human=None) -> None:
    """Move through the shared Bézier HumanInteractor when available."""
    moves = moves or random.randint(1, 3)
    try:
        actor = human or (make_human(page) if make_human is not None else None)
        if actor is not None:
            actor.wander(moves)
            return
    except Exception:
        pass
    try:
        viewport = getattr(page, "viewport_size", None) or {}
    except Exception:
        viewport = {}
    width = max(640, int((viewport or {}).get("width") or 1280))
    height = max(480, int((viewport or {}).get("height") or 800))
    for _ in range(moves):
        try:
            x = random.randint(max(30, int(width * 0.08)), max(31, int(width * 0.92)))
            y = random.randint(max(60, int(height * 0.12)), max(61, int(height * 0.88)))
            page.mouse.move(x, y)
            _sleep(0.2, 0.7)
        except Exception:
            return


def human_read(page, dump: WarmupDump, deadline: float, max_seconds: float,
               visible_actions: bool = False, human=None) -> None:
    """Adaptive reading with explicit progress heartbeats.

    Most activity on a reading page is scrolling and pausing, not continuous
    pointer movement. Diagnostic mode adds regular cursor drift and terminal
    action logs so it is obvious that the worker is alive.
    """
    st = observe(page)
    wpm = random.uniform(200, 260)
    est = (st.word_count / wpm) * 60.0 if st.word_count else random.uniform(8, 20)
    budget = min(max_seconds, max(6.0, est * random.uniform(0.45, 0.9)))
    budget = min(budget, max(2.0, deadline - time.time()))
    end = time.time() + budget
    dump.capture(page, "reading", f"words={st.word_count} dwell={budget:.0f}s",
                 extra={"kind": st.kind, "host": st.host, "phase": "start"})
    action_index = 0
    last_observe = time.time()
    while time.time() < end:
        action_index += 1
        action = random.choices(
            ["scroll_down", "scroll_down", "pause", "scroll_up", "mouse_move"],
            weights=[5, 4, 2, 1, 4 if visible_actions else 2])[0]
        dump.capture(page, "reading_activity", action,
                     extra={"kind": st.kind, "host": st.host, "index": action_index})
        if visible_actions:
            log(f"reading action {action_index}: {action}", "ACT")
        try:
            if action == "scroll_down":
                if human is not None:
                    human.scroll(random.randint(220, 560), direction=1)
                else:
                    page.mouse.wheel(0, random.randint(220, 560))
            elif action == "scroll_up":
                if human is not None:
                    human.scroll(random.randint(100, 280), direction=-1, allow_correction=False)
                else:
                    page.mouse.wheel(0, -random.randint(100, 280))
            elif action == "mouse_move":
                human_mouse(page, moves=1, human=human)
            elif human is not None:
                human.dwell(0.7, 1.7, micro_moves=True)
        except Exception:
            pass
        _sleep(0.55, 1.55)

        # Full DOM observation is deliberately not performed after every wheel
        # event. That was excessive Playwright traffic and made Firefox stalls
        # more likely. Re-check blockers periodically instead.
        if time.time() - last_observe > 8.0:
            s2 = observe(page)
            last_observe = time.time()
            if s2.kind in ("captcha", "blocked"):
                return


def accept_consent(page, dump: WarmupDump, human=None) -> bool:
    """Accept cookies (build a realistic cookie jar for trust). True if handled."""
    continuation = continue_after_dialog(
        page,
        wait_seconds=5.0,
        cookie_action="allow_all_cookies",
    )
    if continuation.get("clicked_action") == "allow_all_cookies":
        dump.capture(
            page,
            "consent_accepted",
            "shared dialog continuation",
            force_snapshot=True,
        )
        return True
    for label in ACCEPT_LABELS:
        for getter in (
            lambda lab=label: page.get_by_role("button", name=re.compile(lab, re.I)),
            lambda lab=label: page.get_by_role("link", name=re.compile(lab, re.I)),
            lambda lab=label: page.locator(f"button:has-text('{lab}')"),
            lambda lab=label: page.locator(f"[aria-label*='{lab}' i]"),
        ):
            try:
                loc = getter().first
                if loc.count() > 0:
                    actor = human or (make_human(page) if make_human is not None else None)
                    clicked = actor.click(loc, timeout=2500) if actor is not None else False
                    if not clicked:
                        human_mouse(page, 1, human=actor)
                        loc.click(timeout=2500)
                    dump.capture(page, "consent_accepted", label, force_snapshot=True)
                    _sleep(0.8, 1.8)
                    return True
            except Exception:
                continue
    return False


def type_like_human(page, locator, text: str, human=None) -> bool:
    try:
        actor = human or (make_human(page) if make_human is not None else None)
        if actor is not None:
            if not actor.type_text(text, locator=locator, clear=True, allow_typos=False):
                return False
            actor.dwell(0.25, 0.65, micro_moves=False)
            return actor.press(locator, "Enter")
        locator.click(timeout=4000)
        _sleep(0.3, 0.9)
        locator.type(text, delay=random.randint(60, 150))
        _sleep(0.3, 0.8)
        locator.press("Enter")
        return True
    except Exception:
        return False


# =====================================================================
#  Persona query generation
# =====================================================================

def _expand(template: str, slots: Dict[str, List[str]]) -> str:
    out = template
    for key, opts in slots.items():
        out = out.replace("{" + key + "}", random.choice(opts))
    return out


class Persona:
    def __init__(self, name: str):
        self.name = name if name in PERSONAS else "generalist"
        self.cfg = PERSONAS[self.name]
        self._used: set = set()

    def search_query(self) -> str:
        for _ in range(6):
            tmpl, slots = random.choice(self.cfg["topics"])
            q = _expand(tmpl, slots) if slots else tmpl
            if q not in self._used:
                self._used.add(q)
                return q
        return random.choice(self.cfg["news"])

    def news_query(self) -> str:
        return random.choice(self.cfg["news"])

    def shop_query(self) -> str:
        return random.choice(self.cfg["shop"])


# =====================================================================
#  WebWarmup — reactive engine
# =====================================================================

ENGINE_HOME = {
    "google": ("https://www.google.com/", "textarea[name='q'], input[name='q']"),
    "bing": ("https://www.bing.com/", "input[name='q'], #sb_form_q"),
    "duckduckgo": ("https://duckduckgo.com/", "input[name='q'], #searchbox_input"),
}


DEBUG_CURSOR_SCRIPT = r"""
(() => {
  if (window.__sparkgridDebugCursorInstalled) return;
  window.__sparkgridDebugCursorInstalled = true;
  const ensure = () => {
    let dot = document.getElementById('__sparkgrid_debug_cursor');
    if (!dot && document.documentElement) {
      dot = document.createElement('div');
      dot.id = '__sparkgrid_debug_cursor';
      dot.setAttribute('aria-hidden', 'true');
      Object.assign(dot.style, {
        position: 'fixed', width: '14px', height: '14px', borderRadius: '50%',
        background: 'rgba(255, 45, 45, 0.82)', border: '2px solid rgba(255,255,255,0.92)',
        boxShadow: '0 1px 5px rgba(0,0,0,0.45)', pointerEvents: 'none',
        zIndex: '2147483647', left: '-30px', top: '-30px',
        transform: 'translate(-50%, -50%)'
      });
      document.documentElement.appendChild(dot);
    }
    return dot;
  };
  document.addEventListener('mousemove', (event) => {
    const dot = ensure();
    if (dot) {
      dot.style.left = event.clientX + 'px';
      dot.style.top = event.clientY + 'px';
    }
  }, true);
  document.addEventListener('mousedown', () => {
    const dot = ensure();
    if (dot) dot.style.transform = 'translate(-50%, -50%) scale(0.72)';
  }, true);
  document.addEventListener('mouseup', () => {
    const dot = ensure();
    if (dot) dot.style.transform = 'translate(-50%, -50%) scale(1)';
  }, true);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensure, {once: true});
  } else {
    ensure();
  }
})();
"""


class WebWarmup:
    def __init__(self, context, dump: WarmupDump, persona: Persona,
                 minutes: float, mode: str = "desktop", profile: str = "", show_cursor: bool = False):
        self.ctx = context
        self.dump = dump
        self.persona = persona
        self.deadline = time.time() + float(minutes) * 60.0
        self.mode = mode
        self.profile = safe_name(profile or getattr(dump, "profile", "") or "web_warmup_test")
        self.pages: List = list(getattr(context, "pages", []) or [])
        if not self.pages:
            self.pages = [context.new_page()]
        self.page = self.pages[0]
        self.show_cursor = bool(show_cursor)
        self.human = make_human(self.page, self.profile) if make_human is not None else None
        self._close_extra_pages()
        if self.show_cursor:
            self._install_debug_cursor()
        prefs = load_browser_preferences(self.profile, self.mode) if load_browser_preferences else {}
        preferred = str(prefs.get("preferred_search_engine") or "google").lower()
        self.preferred_engine = preferred if preferred in SEARCH_ENGINES else "google"
        last_working = str(prefs.get("last_working_search_engine") or "").lower()
        self.last_working_engine = last_working if last_working in SEARCH_ENGINES else ""
        self.engine_health = {k: v["weight"] for k, v in SEARCH_ENGINES.items()}
        self.stats = {"searches": 0, "reads": 0, "shopping": 0, "tabs_opened": 0,
                      "extra_pages_closed": 0, "captcha_hits": 0,
                      "consent_accepted": 0, "preferred_engine": self.preferred_engine,
                      "intents": {}}

    def _install_debug_cursor(self) -> None:
        """Install a visual cursor only for supervised diagnostics.

        It is intentionally opt-in because any DOM overlay, even pointer-events
        none, is unnecessary during production uploads.
        """
        try:
            if hasattr(self.ctx, "add_init_script"):
                self.ctx.add_init_script(DEBUG_CURSOR_SCRIPT)
        except Exception:
            pass
        try:
            self.page.evaluate(DEBUG_CURSOR_SCRIPT)
        except Exception:
            pass

    def _close_extra_pages(self) -> None:
        """Keep one browser page for the whole warm-up session.

        Firefox/Camoufox may render Playwright ``new_page()`` as another native
        window. That creates focus races and is unnecessary for a deterministic
        background warm-up. Popups and stray pages are closed immediately.
        """
        try:
            pages = [p for p in list(getattr(self.ctx, "pages", []) or []) if not p.is_closed()]
        except Exception:
            pages = list(getattr(self.ctx, "pages", []) or [])
        if not pages:
            return
        if self.page not in pages:
            self.page = pages[0]
        for other in list(pages):
            if other is self.page:
                continue
            try:
                other.close()
                if hasattr(self, "stats"):
                    self.stats["extra_pages_closed"] = self.stats.get("extra_pages_closed", 0) + 1
            except Exception:
                pass
        self.pages = [self.page]

    def _remember_engine(self, engine: str) -> None:
        if engine not in SEARCH_ENGINES:
            return
        self.last_working_engine = engine
        if save_browser_preferences:
            try:
                save_browser_preferences(
                    self.profile,
                    self.mode,
                    preferred_search_engine=self.preferred_engine,
                    last_working_search_engine=engine,
                )
            except Exception:
                pass

    # ---- helpers -----------------------------------------------------
    def _time_left(self) -> float:
        return self.deadline - time.time()

    def _goto(self, url: str, timeout: int = 45000) -> PageState:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            if _is_dead_error(exc):
                raise BrowserDead(str(exc)[:200])
            self.dump.capture(self.page, "nav_error", url, error=str(exc)[:200])
        _sleep(1.2, 2.8)
        return self._react_to_blockers()

    def _react_to_blockers(self) -> PageState:
        """HARD RULES: consent + captcha. Re-read after handling."""
        st = observe(self.page)
        if st.kind == "consent":
            if accept_consent(self.page, self.dump, self.human):
                self.stats["consent_accepted"] += 1
            st = observe(self.page)
        if st.kind == "captcha":
            self.stats["captcha_hits"] += 1
            eng = next((e for e in self.engine_health if e in st.host), None)
            if eng:
                self.engine_health[eng] = max(0, self.engine_health[eng] - 5)
            self.dump.capture(self.page, "captcha_detected", st.host,
                              error="bot-check seen; NOT solving, backing off",
                              force_snapshot=True)
            log(f"captcha/bot-check on {st.host} -> backing off, switching source", "WARNING")
            self._goto(random.choice(NEUTRAL_READS))
            st = observe(self.page)
        return st

    def _pick_engine(self) -> str:
        live = {k: w for k, w in self.engine_health.items() if w > 0}
        if not live:
            self.engine_health = {k: v["weight"] for k, v in SEARCH_ENGINES.items()}
            live = dict(self.engine_health)
        # Keep the profile's normal search habit stable. Fallback engines are
        # used only when the preferred engine was disabled for this session.
        if self.preferred_engine in live:
            return self.preferred_engine
        if self.last_working_engine in live:
            return self.last_working_engine
        names, weights = zip(*live.items())
        return random.choices(names, weights=weights)[0]

    # ---- link collection / navigation -------------------------------
    def _collect_links(self, internal: bool) -> List[dict]:
        """Pull anchors from the live DOM. internal=True -> same-site links for
        walking around a site; internal=False -> external organic results."""
        cur_host = _host_of(self.page.url)
        base = ".".join(cur_host.split(".")[-2:]) if cur_host else ""
        try:
            raw = self.page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).slice(0, 400).map(a => {
                    const r = a.getBoundingClientRect();
                    return {href: a.href, text: (a.innerText||'').trim().slice(0,120),
                            vis: (r.width>20 && r.height>10 && r.top<3000 && r.top>-200)};
                })""") or []
        except Exception as exc:
            if _is_dead_error(exc):
                raise BrowserDead(str(exc)[:200])
            return []
        skip_hosts = ("google.", "bing.", "duckduckgo.", "gstatic.", "microsoft.",
                      "facebook.", "twitter.", "x.com", "youtube.", "instagram.")
        out, seen = [], set()
        for a in raw:
            href = str(a.get("href") or "")
            if not href.startswith("http"):
                continue
            if "#" in href and href.split("#")[0] == self.page.url.split("#")[0]:
                continue
            h = _host_of(href)
            same = bool(base) and (h == cur_host or h.endswith("." + base) or h == base)
            if internal and not same:
                continue
            if not internal:
                if same or any(s in h for s in skip_hosts):
                    continue
            text = str(a.get("text") or "")
            if not internal and len(text) < 8:
                continue
            if href in seen:
                continue
            seen.add(href)
            out.append({"href": href, "text": text, "vis": bool(a.get("vis"))})
        # prefer visible links with real text
        out.sort(key=lambda d: (d["vis"], len(d["text"])), reverse=True)
        return out[:25]

    def _go_to_link(self, link: dict, new_tab: bool = False) -> bool:
        """Navigate inside the existing page.

        ``new_tab`` is accepted for backward compatibility but intentionally
        ignored: Runtime v2.3 uses one native window and one page throughout.
        """
        href, text = link["href"], (link.get("text") or "")
        before = self.page.url
        if text:
            try:
                loc = self.page.get_by_role("link", name=re.compile(re.escape(text[:40]), re.I)).first
                if loc.count() > 0:
                    if self.human is not None:
                        self.human.hover(loc, timeout=2000)
                        self.human.dwell(0.18, 0.65, micro_moves=False)
                        self.human.click(loc, timeout=4000)
                    else:
                        loc.hover(timeout=2000)
                        _sleep(0.3, 1.0)
                        loc.click(timeout=4000)
                    _sleep(1.5, 3.0)
                    self._close_extra_pages()
                    if self.page.url != before:
                        self._react_to_blockers()
                        return True
            except Exception as exc:
                if _is_dead_error(exc):
                    raise BrowserDead(str(exc)[:200])
        self._goto(href)
        self._close_extra_pages()
        return self.page.url != before

    def enter_site_from_serp(self, result_sel: str, new_tab_prob: float = 0.0) -> bool:
        """From a search results page, land on a real external site."""
        engine_host = _host_of(self.page.url)
        # hover a couple of result titles first (human scan)
        try:
            titles = self.page.locator(result_sel)
            n = min(titles.count(), 6)
            for _ in range(random.randint(1, max(1, min(3, n)))):
                title = titles.nth(random.randint(0, max(0, n - 1)))
                if self.human is not None:
                    self.human.hover(title, timeout=2000)
                    self.human.dwell(0.25, 0.85, micro_moves=True)
                else:
                    title.hover(timeout=2000)
                    _sleep(0.4, 1.2)
        except Exception:
            pass
        results = self._collect_links(internal=False)
        if not results:
            self.dump.capture(self.page, "no_results", f"host={engine_host}")
            return False
        # humans favour the top few organic results
        choice = random.choice(results[:min(5, len(results))])
        landed = self._go_to_link(choice, new_tab=False)
        st = observe(self.page)
        self.dump.capture(self.page, "entered_site", _host_of(self.page.url),
                          extra={"from": engine_host, "kind": st.kind}, force_snapshot=True)
        log(f"entered site: {_host_of(self.page.url)} ({st.kind})", "ACT")
        return landed and _host_of(self.page.url) != engine_host

    def browse_in_site(self, max_seconds: float, depth: Tuple[int, int] = (2, 4)) -> None:
        """Walk around the current site adaptively: read, click internal links,
        go a few pages deep, occasionally open an internal link in a new tab."""
        pages_to_visit = random.randint(*depth)
        landing_host = _host_of(self.page.url)
        for step in range(pages_to_visit):
            if self._time_left() < 12:
                break
            st = self._react_to_blockers()
            if st.kind in ("captcha",):
                return
            # read the current page (adaptive dwell)
            per_page = min(max_seconds, max(8.0, self._time_left() * 0.3))
            human_read(self.page, self.dump, self.deadline, max_seconds=per_page,
                       visible_actions=self.show_cursor, human=self.human)
            self.stats["reads"] += 1
            self.stats["pages_in_site"] = self.stats.get("pages_in_site", 0) + 1
            # find internal links to go deeper
            internal = self._collect_links(internal=True)
            if not internal:
                self.dump.capture(self.page, "no_internal_links", _host_of(self.page.url))
                break
            link = random.choice(internal[:min(12, len(internal))])
            self.dump.capture(self.page, "in_site_nav",
                              f"step={step+1}/{pages_to_visit} click: "
                              f"{(link.get('text') or link['href'])[:60]}",
                              extra={"host": _host_of(self.page.url)})
            self._go_to_link(link, new_tab=False)
            _sleep(1.2, 3.0)
        self._close_extra_pages()

    # ---- intents -----------------------------------------------------
    def intent_search_and_read(self) -> None:
        engine = self._pick_engine()
        query = self.persona.search_query() if random.random() < 0.7 else self.persona.news_query()
        home, input_sel = ENGINE_HOME[engine]
        self.dump.capture(self.page, "search_open", f"{engine}: {query}")
        st = self._goto(home)
        if st.kind == "captcha":
            return
        typed = False
        try:
            box = self.page.locator(input_sel).first
            if box.count() > 0:
                typed = type_like_human(self.page, box, query, self.human)
        except Exception:
            typed = False
        if not typed:   # fallback: direct query URL
            self._goto(SEARCH_ENGINES[engine]["search_url"].format(q=query.replace(" ", "+")))
        _sleep(1.5, 3.0)
        st = self._react_to_blockers()
        self.stats["searches"] += 1
        if st.kind == "captcha":
            return
        self._remember_engine(engine)
        # scan SERP, then actually land on a result site and walk around it
        human_mouse(self.page, 2, self.human)
        try:
            if self.human is not None:
                self.human.scroll(random.randint(200, 500), direction=1)
            else:
                self.page.mouse.wheel(0, random.randint(200, 500))
        except Exception:
            pass
        _sleep(1.0, 2.5)
        if self.enter_site_from_serp(SEARCH_ENGINES[engine]["result_sel"]):
            self.browse_in_site(max_seconds=min(60, max(15, self._time_left() * 0.4)))
            # sometimes go back to the SERP and open a second site
            if random.random() < 0.4 and self._time_left() > 40:
                try:
                    self.page.go_back(timeout=8000)
                    _sleep(1.0, 2.0)
                    self._react_to_blockers()
                    if self.enter_site_from_serp(SEARCH_ENGINES[engine]["result_sel"], new_tab_prob=0.2):
                        self.browse_in_site(max_seconds=min(40, max(10, self._time_left() * 0.3)),
                                            depth=(1, 2))
                except BrowserDead:
                    raise
                except Exception:
                    pass

    def intent_visit_site(self) -> None:
        """Go straight to a real website and browse around inside it."""
        pool = CONTENT_SITES.get(self.persona.name, CONTENT_SITES["generalist"])
        url = random.choice(pool)
        self.dump.capture(self.page, "visit_site", url)
        log(f"visiting site: {url}", "ACT")
        st = self._goto(url)
        if st.kind == "captcha":
            return
        self.browse_in_site(max_seconds=min(70, max(15, self._time_left() * 0.45)),
                            depth=(2, 4))

    def intent_browse_shopping(self) -> None:
        q = self.persona.shop_query()
        self.dump.capture(self.page, "shopping_open", q)
        st = self._goto(GOOGLE_SHOPPING.format(q=q.replace(" ", "+")))
        self.stats["shopping"] += 1
        if st.kind == "captcha":
            return
        # browse the product grid: hover cards, scroll, peek a product
        for _ in range(random.randint(2, 4)):
            human_mouse(self.page, random.randint(1, 2), self.human)
            try:
                if self.human is not None:
                    self.human.scroll(random.randint(300, 700), direction=1)
                else:
                    self.page.mouse.wheel(0, random.randint(300, 700))
            except Exception:
                pass
            _sleep(1.4, 3.4)
            if observe(self.page).kind == "captcha":
                self._react_to_blockers()
                return
        # open a product/merchant site and look at it
        if self.enter_site_from_serp("a[href*='/shopping/product'], a:has(h3), div a h3",
                                     new_tab_prob=0.4):
            self.browse_in_site(max_seconds=min(40, max(10, self._time_left() * 0.25)),
                                depth=(1, 3))

    def intent_read_neutral(self) -> None:
        self.dump.capture(self.page, "neutral_open", "")
        self._goto(random.choice(NEUTRAL_READS))
        self.browse_in_site(max_seconds=min(50, max(12, self._time_left() * 0.35)),
                            depth=(1, 3))

    def intent_tab_juggle(self) -> None:
        """Single-page idle behavior kept under the legacy method name.

        We intentionally do not create or focus extra pages. A short cursor
        drift, partial scroll and optional back/forward revisit provides variety
        without native-window flashes.
        """
        self._close_extra_pages()
        try:
            human_mouse(self.page, 2, self.human)
            if self.human is not None:
                self.human.scroll(random.randint(120, 420), direction=1)
            else:
                self.page.mouse.wheel(0, random.randint(120, 420))
            _sleep(1.0, 2.5)
            if random.random() < 0.25:
                self.page.go_back(timeout=8000)
                _sleep(0.8, 1.8)
                self._react_to_blockers()
        except BrowserDead:
            raise
        except Exception:
            pass

    # ---- director / main loop ---------------------------------------
    def _choose_intent(self) -> Callable[[], None]:
        intents = [
            (self.intent_search_and_read, 6),   # search -> land on a real result site -> walk it (most varied)
            (self.intent_browse_shopping, 4),    # search products -> real merchant sites
            (self.intent_visit_site, 3),         # go straight to a real site -> walk it
            (self.intent_read_neutral, 1),
            (self.intent_tab_juggle, 1),
        ]
        fns, weights = zip(*intents)
        return random.choices(fns, weights=weights)[0]

    def run(self) -> Dict:
        self._close_extra_pages()
        self.dump.capture(self.page, "warmup_start",
                          f"persona={self.persona.name} minutes={(self.deadline - time.time())/60:.1f}",
                          force_snapshot=True)
        # warm start on a CALM page (Wikipedia rarely throws uncaught JS that
        # trips the FF driver), then walk it, before heavier sites/search.
        log("warm-up start: opening a neutral page", "ACT")
        self._goto("https://en.wikipedia.org/wiki/Special:Random")
        if self.show_cursor:
            self._install_debug_cursor()
        log("neutral page loaded: mouse, scroll and reading phase", "ACT")
        human_mouse(self.page, 3 if self.show_cursor else 2, self.human)
        try:
            if self.human is not None:
                self.human.scroll(random.randint(180, 420), direction=1)
            else:
                self.page.mouse.wheel(0, random.randint(180, 420))
        except Exception:
            pass
        # A two-minute diagnostic should reach a real intent quickly instead of
        # spending almost the whole run on the neutral landing page.
        short_session = self._time_left() < 180
        self.browse_in_site(max_seconds=12 if short_session else 20, depth=(1, 1))
        cycle = 0
        while self._time_left() > 5:
            cycle += 1
            intent = self._choose_intent()
            name = intent.__name__
            self.stats["intents"][name] = self.stats["intents"].get(name, 0) + 1
            self.dump.capture(self.page, "intent", name,
                              extra={"cycle": cycle, "time_left_s": round(self._time_left())})
            log(f"cycle {cycle}: {name} (left {self._time_left()/60:.1f}m)", "ACT")
            try:
                intent()
            except BrowserDead:
                raise                       # let the supervisor relaunch & resume
            except Exception as exc:
                self.dump.capture(self.page, "intent_error", name, error=str(exc)[:200])
            self._close_extra_pages()
            # inter-intent idle (humans pause between tasks)
            _sleep(2.0, 6.0)
        self.dump.capture(self.page, "warmup_done", json.dumps(self.stats, ensure_ascii=False),
                          extra={"stats": self.stats}, force_snapshot=True)
        log(f"warmup finished: {json.dumps(self.stats, ensure_ascii=False)}", "OK")
        return {"ok": True, "stats": self.stats}


# =====================================================================
#  Camoufox context (real) + proxy parsing
# =====================================================================

def parse_proxy(proxy: str):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    if "://" in proxy:
        from urllib.parse import urlparse
        u = urlparse(proxy)
        if u.hostname and u.port:
            out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
            if u.username:
                out["username"] = u.username
            if u.password:
                out["password"] = u.password
            return out
        return {"server": proxy}
    parts = proxy.split(":")
    if len(parts) == 4:
        h, p, us, pw = parts
        return {"server": f"http://{h}:{p}", "username": us, "password": pw}
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    return {"server": proxy}


def storage_path(profile: str, mode: str) -> Path:
    if sparkbrowser_state_path:
        return sparkbrowser_state_path(profile, "", mode)
    p = PROFILE_ROOT / safe_name(profile) / mode
    p.mkdir(parents=True, exist_ok=True)
    return p / "camoufox_storage_state.json"


def open_camoufox(profile: str, mode: str, headless: bool, proxy: str, locale: str):
    """Open Camoufox and a persistent-state context. Returns (cm, context)."""
    if not open_spark_browser:
        raise RuntimeError("SparkBrowser launcher is not available")
    cm, context, _page = open_spark_browser(profile, proxy, mode=mode, headless=headless, locale=locale, humanize=False)
    try:
        setattr(context, "_sparkgrid_proxy", proxy)
    except Exception:
        pass
    return cm, context


def save_state(context, profile: str, mode: str) -> str:
    try:
        proxy = getattr(context, "_sparkgrid_proxy", "")
        if save_browser_state:
            return save_browser_state(context, profile, proxy, mode)
        sp = storage_path(profile, mode)
        context.storage_state(path=str(sp))
        return str(sp)
    except Exception:
        return ""


# =====================================================================
#  Dry-run mocks — validate the reactive engine offline (no browser)
# =====================================================================

class _FakeMouse:
    def move(self, *a, **k): pass
    def wheel(self, *a, **k): self._p._y += abs(a[1]) if len(a) > 1 else 0
    def __init__(self, page): self._p = page


class _FakeLocator:
    def __init__(self, n=10, text=""): self._n = n; self._text = text
    def count(self): return self._n
    def nth(self, i): return self
    @property
    def first(self): return self
    def hover(self, **k): pass
    def click(self, **k): pass
    def type(self, *a, **k): pass
    def press(self, *a, **k): pass
    def inner_text(self, **k): return self._text


class _FakePage:
    """Cycles through page kinds so every rule/intent path is exercised."""
    _SCRIPT = ["serp", "article", "consent", "shopping", "captcha", "article", "serp"]

    def __init__(self): self.url = "https://example.com/page1"; self._y = 0; self._i = 0; self.mouse = _FakeMouse(self)
    def goto(self, url, **k):
        self.url = url
        self._i = (self._i + 1) % len(self._SCRIPT)
    def title(self): return "Fake Page"
    def screenshot(self, **k): pass
    def bring_to_front(self): pass
    def go_back(self, **k): pass
    def close(self): pass
    def evaluate(self, script, *a, **k):
        if "href" in str(script):  # _collect_links anchor harvest
            host = _host_of(self.url) or "example.com"
            base = ".".join(host.split(".")[-2:])
            return [
                {"href": f"https://{base}/article-{n}", "text": f"Internal link {n} long text", "vis": True}
                for n in range(1, 6)
            ] + [
                {"href": f"https://news-{n}.com/story", "text": f"External result {n} headline", "vis": True}
                for n in range(1, 4)
            ]
        return {"y": self._y, "h": 4000, "vh": 900, "n": 30}
    def locator(self, sel):
        kind = self._SCRIPT[self._i]
        if "body" in sel:
            texts = {"serp": "results " * 400, "article": "lorem ipsum " * 600,
                     "consent": "we use cookies accept all privacy gdpr", "shopping": "shop tbm price " * 200,
                     "captcha": "unusual traffic detected are you a robot recaptcha"}
            return _FakeLocator(text=texts.get(kind, "hello world " * 300))
        return _FakeLocator(n=10)
    def get_by_role(self, *a, **k): return _FakeLocator(n=1)
    def mouse_wheel(self, *a): pass


class _FakeContext:
    def __init__(self): self.pages = [_FakePage()]
    def new_page(self): p = _FakePage(); self.pages.append(p); return p
    def storage_state(self, **k): return {}


# =====================================================================
#  CLI
# =====================================================================

def main() -> int:
    global PROFILE_ROOT
    ap = argparse.ArgumentParser(description="Standalone Camoufox general web warmup (no Instagram)")
    ap.add_argument("--minutes", type=float, default=8.0)
    ap.add_argument("--profile", default="web_warmup_test", help="Single profile name")
    ap.add_argument("--profiles", default="", help="Batch: comma/space separated profile names")
    ap.add_argument("--profiles-file", default="", help="Batch: file with one 'profile' or 'profile|proxy' per line")
    ap.add_argument("--from-db", action="store_true", help="Batch: pull enabled profiles+proxies from bot.db accounts")
    ap.add_argument("--parallel", type=int, default=3, help="Batch: max concurrent browsers (keep low for headed)")
    ap.add_argument("--persona", choices=list(PERSONAS.keys()) + ["random"], default="generalist")
    ap.add_argument("--mode", choices=["desktop", "mobile_like"], default="desktop")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--max-snapshots", type=int, default=30)
    ap.add_argument("--show-cursor", action="store_true", help="Diagnostic only: show a red cursor overlay and action logs")
    ap.add_argument("--debug-screenshots", action="store_true",
                    help="Diagnostic only: save bounded screenshots on state changes; disabled by default")
    ap.add_argument("--profile-root", default="", help="Override profile root; UI uses IG Web Upload profiles")
    ap.add_argument("--mark-db", action="store_true", help="Mark accounts as web_warmup_done / web_warmup_failed in bot.db")
    ap.add_argument("--dry-run", action="store_true", help="Offline self-test, no browser")
    # internal (supervisor -> child); not for manual use
    ap.add_argument("--run-id", default="")
    ap.add_argument("--_until", type=float, default=0.0)
    args = ap.parse_args()
    if args.profile_root:
        PROFILE_ROOT = Path(args.profile_root).expanduser().resolve()

    persona = Persona(_pick_persona(args.persona))

    batch_requested = bool(args.profiles or args.profiles_file or args.from_db)

    # SUPERVISOR / ORCHESTRATOR path: spawn child sessions. No dump here.
    if not args.dry_run and not (args._until and args._until > 0):
        if batch_requested:
            return _orchestrate_batch(args)
        log(f"profile={args.profile} persona={persona.name} minutes={args.minutes} "
            f"mode={args.mode} (supervised)", "OK")
        return _supervise(args)

    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    dump = WarmupDump(run_id, args.profile, max_snapshots=args.max_snapshots,
                      screenshots=getattr(args, "debug_screenshots", False))
    log(f"run_id={run_id} profile={args.profile} persona={persona.name} "
        f"minutes={args.minutes} mode={args.mode} dry_run={args.dry_run}", "OK")

    if args.dry_run:
        ctx = _FakeContext()
        # speed up the loop for the self-test: no sleeps, no real-time reads
        global _sleep, human_read
        _orig_sleep, _orig_read = _sleep, human_read
        def _fast(a, b): pass
        def _fast_read(page, dump, deadline, max_seconds, **kwargs):
            st = observe(page)
            dump.capture(page, "reading", f"words={st.word_count} (dry)")
        _sleep = _fast            # type: ignore
        human_read = _fast_read   # type: ignore
        try:
            engine = WebWarmup(ctx, dump, persona, minutes=args.minutes, mode=args.mode, profile=args.profile, show_cursor=getattr(args, "show_cursor", False))
            engine.deadline = time.time() + 0.001  # one symbolic pass is enough; we force cycles below
            # force a deterministic number of cycles so all paths run
            engine.deadline = time.time() + 9999
            cycles = 0
            for fn in (engine.intent_visit_site, engine.intent_search_and_read,
                       engine.intent_browse_shopping, engine.intent_read_neutral,
                       engine.intent_tab_juggle, engine.intent_visit_site):
                fn(); cycles += 1
            engine.dump.capture(engine.page, "warmup_done",
                                json.dumps(engine.stats, ensure_ascii=False),
                                extra={"stats": engine.stats}, force_snapshot=True)
            log(f"DRY-RUN ok: {cycles} intents exercised; stats={json.dumps(engine.stats, ensure_ascii=False)}", "OK")
        finally:
            _sleep = _orig_sleep      # type: ignore
            human_read = _orig_read   # type: ignore
        print(f"\nDebug artifacts: {dump.root}")
        return 0

    # live mode: this process is either the SUPERVISOR (default) or a single
    # CHILD session (when --_until is passed by the supervisor).
    if args._until and args._until > 0:
        return _child_session(args, dump, persona)
    return _supervise(args)


def _start_child_watchdog(until: float, grace: float = 90.0) -> None:
    """Self-defence for the child: a daemon thread that force-exits this process
    if it outlives its deadline (even while blocked forever inside a browser
    call) OR if it is orphaned (the supervisor died, e.g. on a UI restart).
    Without this a hung child can survive for days (seen 2026-06-06)."""
    import threading

    def _guard():
        while True:
            time.sleep(5.0)
            try:
                if until and time.time() > float(until) + grace:
                    log("watchdog: deadline+grace exceeded; force-exiting child.", "WARNING")
                    os._exit(0)
                if os.getppid() == 1:  # reparented to init/launchd => supervisor gone
                    log("watchdog: orphaned (supervisor gone); force-exiting child.", "WARNING")
                    os._exit(0)
            except Exception:
                pass

    threading.Thread(target=_guard, daemon=True).start()


def _child_session(args, dump: "WarmupDump", persona: "Persona") -> int:
    """Run exactly one Camoufox launch + warmup, bounded by args._until.
    Exit codes: 0 = finished, 17 = browser/driver crash (parent relaunches),
    1 = other fatal error."""
    _start_child_watchdog(args._until)
    cm = context = None
    try:
        cm, context = open_camoufox(args.profile, args.mode, args.headless, args.proxy, args.locale)
        log(f"SparkBrowser opened; storage_state={storage_path(args.profile, args.mode)}", "OK")
        remaining_min = max(0.1, (args._until - time.time()) / 60.0)
        engine = WebWarmup(context, dump, persona, minutes=remaining_min, mode=args.mode, profile=args.profile, show_cursor=getattr(args, "show_cursor", False))
        engine.deadline = args._until
        result = engine.run()
        save_state(context, args.profile, args.mode)
        log(f"state saved; {json.dumps(result.get('stats', {}), ensure_ascii=False)}", "OK")
        return 0
    except BrowserDead as exc:
        log(f"FF driver crashed: {exc}", "WARNING")
        try:
            if context:
                save_state(context, args.profile, args.mode)
        except Exception:
            pass
        return 17
    except Exception as exc:
        message = str(exc)
        log(f"session error: {message}", "ERROR")
        try:
            if context:
                save_state(context, args.profile, args.mode)
        except Exception:
            pass
        deterministic = (
            "No headers based on this input can be generated" in message
            or "fingerprint could not be created" in message
            or "fingerprint helper failed" in message
        )
        return 22 if deterministic else 1
    finally:
        try:
            if cm:
                cm.__exit__(None, None, None)
        except Exception:
            pass


def _pick_persona(name: str) -> str:
    return random.choice(list(PERSONAS.keys())) if name == "random" else (
        name if name in PERSONAS else "generalist")


def _heartbeat_path(run_id: str, profile: str) -> Path:
    # the child's WarmupDump rewrites latest_state.json on every captured action,
    # so its mtime is a real "browser made progress" heartbeat.
    return DEBUG_ROOT / run_id / safe_name(profile) / "latest_state.json"


# watchdog tuning
_LAUNCH_GRACE = 130   # s allowed for Camoufox launch before first heartbeat
_STALE_SECS = 75      # s without progress (with browser up) => hung => kill


def _wait_child(popen, hb_path: Path, hard_deadline: float):
    """Block until the child exits, hangs (stale heartbeat), or hits the deadline.
    Returns one of: ('exit', rc), ('stale', None), ('timeout', None)."""
    started = time.time()
    while True:
        rc = popen.poll()
        if rc is not None:
            return ("exit", rc)
        now = time.time()
        if now >= hard_deadline:
            _stop_gracefully(popen)
            return ("timeout", None)
        try:
            hb = hb_path.stat().st_mtime
        except Exception:
            hb = 0.0
        if hb <= started:                         # no FRESH heartbeat since this child launched
            if now - started > _LAUNCH_GRACE:     # never produced a heartbeat (ignores stale files from prior attempts)
                _kill(popen)
                return ("stale", None)
        elif now - hb > _STALE_SECS:              # browser was up, then froze
            _kill(popen)
            return ("stale", None)
        time.sleep(2.0)


def _stop_gracefully(popen, wait_seconds: float = 8.0) -> None:
    """Ask the child/browser tree to stop on the current operating system."""
    stop_process_tree(popen, graceful_timeout=wait_seconds)


def _kill(popen) -> None:
    """Force-stop the child and its browser subtree cross-platform."""
    stop_process_tree(popen, graceful_timeout=0.5)


def _child_base_cmd(profile: str, proxy: str, persona: str, args, run_id: str) -> List[str]:
    cmd = [sys.executable, "-u", os.path.abspath(__file__),
           "--profile", profile, "--persona", persona, "--mode", args.mode,
           "--locale", args.locale, "--max-snapshots", str(args.max_snapshots),
           "--minutes", str(args.minutes),
           "--run-id", run_id]
    if getattr(args, "profile_root", ""):
        cmd += ["--profile-root", str(args.profile_root)]
    if proxy:
        cmd += ["--proxy", proxy]
    if args.headless:
        cmd += ["--headless"]
    if getattr(args, "show_cursor", False):
        cmd += ["--show-cursor"]
    if getattr(args, "debug_screenshots", False):
        cmd += ["--debug-screenshots"]
    return cmd


def _db_profiles() -> List[Tuple[str, str]]:
    """Pull (name, proxy) for enabled profiles from bot.db accounts, if present."""
    import sqlite3
    dbp = DATA_ROOT / "bot.db"
    if not dbp.exists():
        log("bot.db not found; --from-db has nothing to read", "WARNING")
        return []
    out: List[Tuple[str, str]] = []
    try:
        con = sqlite3.connect(str(dbp), timeout=20)
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        proxy_col = "proxy" if "proxy" in cols else ("proxy_url" if "proxy_url" in cols else "")
        proxy_expr = ("COALESCE(" + proxy_col + ",'')") if proxy_col else "''"
        sql = "SELECT name, " + proxy_expr + " AS proxy FROM accounts WHERE COALESCE(enabled,1)=1 ORDER BY name"
        rows = con.execute(sql).fetchall()
        for r in rows:
            if r["name"]:
                out.append((str(r["name"]).strip(), str(r["proxy"] or "")))
        con.close()
    except Exception as exc:
        log(f"--from-db read failed: {exc}", "WARNING")
    return out


def _mark_db_web_warm(profile: str, ok: bool, error: str = "") -> None:
    """Optional UI integration: record pure web warmup status on the account row."""
    import sqlite3
    dbp = DATA_ROOT / "bot.db"
    if not dbp.exists() or not profile:
        return
    try:
        con = sqlite3.connect(str(dbp), timeout=20)
        cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        if "web_upload_cookie_status" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN web_upload_cookie_status TEXT NOT NULL DEFAULT ''")
        if "web_upload_last_error" not in cols:
            con.execute("ALTER TABLE accounts ADD COLUMN web_upload_last_error TEXT NOT NULL DEFAULT ''")
        if "updated_at" in cols:
            con.execute(
                "UPDATE accounts SET web_upload_cookie_status=?, web_upload_last_error=?, updated_at=datetime('now') WHERE name=?",
                ("web_warmup_done" if ok else "web_warmup_failed", "" if ok else str(error or "web warmup failed"), profile),
            )
        else:
            con.execute(
                "UPDATE accounts SET web_upload_cookie_status=?, web_upload_last_error=? WHERE name=?",
                ("web_warmup_done" if ok else "web_warmup_failed", "" if ok else str(error or "web warmup failed"), profile),
            )
        con.commit()
        con.close()
    except Exception as exc:
        log(f"could not mark DB web warmup status for {profile}: {exc}", "WARNING")


def _resolve_profiles(args) -> List[Tuple[str, str]]:
    """Build the (profile, proxy) work list from --profiles / --profiles-file / --from-db."""
    out: List[Tuple[str, str]] = []
    seen = set()

    def add(name: str, proxy: str = ""):
        name = (name or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append((name, proxy or ""))

    if args.profiles_file:
        try:
            for line in Path(args.profiles_file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:           # profile|proxy   (proxy may contain ':')
                    nm, px = line.split("|", 1)
                    add(nm, px)
                else:
                    add(line)
        except Exception as exc:
            log(f"could not read --profiles-file: {exc}", "ERROR")
    if args.profiles:
        for tok in re.split(r"[,\s]+", args.profiles):
            add(tok)
    if args.from_db:
        for nm, px in _db_profiles():
            add(nm, px)
    return out


def _orchestrate_batch(args) -> int:
    """Warm MANY profiles with bounded concurrency. Each profile gets its own
    Camoufox profile + proxy + per-profile crash-relaunch, full --minutes budget.
    Concurrency is capped by --parallel so 100 profiles don't open 100 browsers."""
    import subprocess

    profiles = _resolve_profiles(args)
    if not profiles:
        log("no profiles resolved (use --profiles, --profiles-file or --from-db)", "ERROR")
        return 1
    parallel = max(1, int(args.parallel))
    if not args.headless and parallel > 6:
        log(f"--parallel {parallel} is high for headed mode; consider <=6 or --headless", "WARNING")

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_dir = ROOT / "browser_warmup_data" / "batch" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    per_min = float(args.minutes)
    waves = (len(profiles) + parallel - 1) // parallel
    eta_min = waves * per_min
    log(f"BATCH {batch_id}: {len(profiles)} profiles, parallel={parallel}, "
        f"{per_min:.0f}m each  ~ETA {eta_min:.0f}m ({eta_min/60:.1f}h)", "OK")

    MAX_RELAUNCH = 6
    status: Dict[str, dict] = {nm: {"state": "queued", "proxy": bool(px), "relaunches": 0,
                                    "started_at": "", "finished_at": ""}
                               for nm, px in profiles}
    status_path = batch_dir / "status.json"

    def write_status():
        done = sum(1 for s in status.values() if s["state"] == "done")
        failed = sum(1 for s in status.values() if s["state"] == "failed")
        running = sum(1 for s in status.values() if s["state"] == "running")
        meta = {"batch_id": batch_id, "total": len(profiles), "done": done,
                "failed": failed, "running": running,
                "queued": len(profiles) - done - failed - running,
                "parallel": parallel, "minutes_each": per_min, "updated": now_iso()}
        try:
            status_path.write_text(json.dumps({"meta": meta, "profiles": status},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return meta

    queue = list(profiles)
    running: Dict[str, dict] = {}   # name -> {popen, proxy, persona, deadline, relaunches, log}

    def spawn(name: str, proxy: str, persona: str, deadline: float, relaunches: int):
        run_id = f"{batch_id}_{safe_name(name)}"
        # Drop any stale heartbeat from a previous (killed) attempt so the fresh
        # child gets its full launch grace instead of being judged hung instantly.
        try:
            _heartbeat_path(run_id, name).unlink()
        except Exception:
            pass
        cmd = _child_base_cmd(name, proxy, persona, args, run_id) + ["--_until", str(deadline)]
        logf = open(batch_dir / f"{safe_name(name)}.log", "ab")
        logf.write(f"\n===== spawn {now_iso()} relaunch={relaunches} =====\n".encode())
        logf.flush()
        popen = subprocess.Popen(cmd, stdout=logf, stderr=logf, **process_group_kwargs())
        running[name] = {"popen": popen, "proxy": proxy, "persona": persona,
                         "deadline": deadline, "relaunches": relaunches, "log": logf,
                         "hb": _heartbeat_path(run_id, name), "spawned": time.time()}
        status[name].update(state="running", relaunches=relaunches,
                            started_at=status[name]["started_at"] or now_iso())

    def _finish(name, info, kind, rc):
        try:
            info["log"].close()
        except Exception:
            pass
        running.pop(name, None)
        if kind == "done":
            status[name].update(state="done", finished_at=now_iso())
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(name, True)
            log(f"done '{name}' (rc={rc})", "OK")
            return
        rel = info["relaunches"] + 1
        reason = "hung" if kind == "stale" else f"rc={rc}"
        if rc == 22:
            status[name].update(state="failed", finished_at=now_iso(), relaunches=info["relaunches"])
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(name, False, "deterministic fingerprint configuration error")
            log(f"FAILED '{name}': deterministic fingerprint/header configuration error", "ERROR")
            return
        if rel > MAX_RELAUNCH:
            status[name].update(state="failed", finished_at=now_iso(), relaunches=rel)
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(name, False, reason)
            log(f"FAILED '{name}' after {MAX_RELAUNCH} failures ({reason})", "ERROR")
        else:
            log(f"{('hang' if kind=='stale' else 'crash')} '{name}' {reason}; "
                f"relaunch {rel}/{MAX_RELAUNCH} ({(info['deadline']-time.time())/60:.1f}m left)", "WARNING")
            spawn(name, info["proxy"], info["persona"], info["deadline"], rel)

    try:
        while queue or running:
            # fill free slots
            while queue and len(running) < parallel:
                name, proxy = queue.pop(0)
                persona = _pick_persona(args.persona)
                spawn(name, proxy, persona, time.time() + per_min * 60.0, 0)
                meta = write_status()
                log(f"[{meta['done']+meta['failed']}/{meta['total']}] start '{name}' "
                    f"(persona={persona}, proxy={'yes' if proxy else 'no'}); running={len(running)}", "ACT")
            # poll running: handle exit, time-budget, and HANGS (stale heartbeat)
            now = time.time()
            for name, info in list(running.items()):
                rc = info["popen"].poll()
                if rc is not None:
                    kind = "done" if (rc == 0 or now >= info["deadline"] - 12) else "crash"
                    _finish(name, info, kind, rc)
                    write_status()
                    continue
                if now >= info["deadline"]:                 # over budget -> stop child, count done
                    _kill(info["popen"])
                    _finish(name, info, "done", 0)
                    write_status()
                    continue
                # hang detection
                try:
                    hbm = info["hb"].stat().st_mtime
                except Exception:
                    hbm = 0.0
                hung = (hbm <= 0.0 and now - info["spawned"] > _LAUNCH_GRACE) or \
                       (hbm > 0.0 and now - hbm > _STALE_SECS)
                if hung:
                    _kill(info["popen"])
                    _finish(name, info, "stale", None)
                    write_status()
            time.sleep(1.5)
    except KeyboardInterrupt:
        log("interrupted; terminating child sessions...", "WARNING")
        for info in running.values():
            _kill(info["popen"])
        write_status()
        return 0

    meta = write_status()
    log(f"BATCH done: {meta['done']} ok, {meta['failed']} failed of {meta['total']}. "
        f"status -> {status_path}", "OK")
    return 0 if meta["failed"] == 0 else 1


def _raise_keyboard_interrupt(_signum=None, _frame=None) -> None:
    """Treat Ctrl+Z like a graceful stop instead of suspending Playwright."""
    raise KeyboardInterrupt


def _install_terminal_stop_handler() -> None:
    try:
        import signal
        if hasattr(signal, "SIGTSTP"):
            signal.signal(signal.SIGTSTP, _raise_keyboard_interrupt)
    except Exception:
        pass


def _supervise(args) -> int:
    """Parent supervisor for a SINGLE profile. Runs each warmup as a child
    subprocess and relaunches on crash OR hang (stale heartbeat), until the
    time budget is spent. Survives hard driver crashes and frozen sessions."""
    import subprocess
    _install_terminal_stop_handler()
    overall_deadline = time.time() + float(args.minutes) * 60.0
    persona = _pick_persona(args.persona)

    relaunches = 0
    MAX_RELAUNCH = 8
    while time.time() < overall_deadline - 12:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        cmd = _child_base_cmd(args.profile, args.proxy, persona, args, run_id) + \
            ["--_until", str(overall_deadline)]
        hb = _heartbeat_path(run_id, args.profile)
        log(f"launching warmup session ({(overall_deadline - time.time())/60:.1f}m left, "
            f"relaunch {relaunches}/{MAX_RELAUNCH})", "OK")
        try:
            popen = subprocess.Popen(cmd, **process_group_kwargs())
        except Exception as exc:
            log(f"could not start session: {exc}", "ERROR")
            return 1
        try:
            kind, rc = _wait_child(popen, hb, overall_deadline)
        except KeyboardInterrupt:
            _stop_gracefully(popen)
            log("interrupted by user", "WARNING")
            return 0
        if kind == "exit" and rc == 0:
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(args.profile, True)
            log("warmup finished normally", "OK")
            return 0
        if kind == "exit" and rc == 22:
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(args.profile, False, "deterministic fingerprint configuration error")
            log("deterministic fingerprint/header configuration error; not relaunching the same broken session", "ERROR")
            return 1
        if kind == "timeout":
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(args.profile, True)
            log("time budget reached; warmup complete", "OK")
            return 0
        relaunches += 1
        reason = "hung (no progress)" if kind == "stale" else f"crash rc={rc}"
        if relaunches > MAX_RELAUNCH:
            if getattr(args, "mark_db", False):
                _mark_db_web_warm(args.profile, False, reason)
            log(f"too many SparkBrowser failures ({reason}); stopping. "
                "Reinstall SparkGrid if the bundled browser runtime is incomplete.", "ERROR")
            return 1
        log(f"session {reason}; killed + relaunching to resume warmup", "WARNING")
        time.sleep(2.0)
    log("time budget reached; warmup complete", "OK")
    if getattr(args, "mark_db", False):
        _mark_db_web_warm(args.profile, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
