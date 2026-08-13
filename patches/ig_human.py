"""Deterministic, bounded human-style Playwright interactions for SparkBrowser.

This module changes only input delivery. It does not modify browser identity,
network settings, cookies, or page content. The API remains compatible with the
older ``ig_human.Human`` helper used by the Instagram workflow and uploader.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import secrets
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class HumanActionProfile:
    name: str = "balanced"
    speed: float = 1.0
    min_move_steps: int = 14
    max_move_steps: int = 48
    click_hold_min: float = 0.045
    click_hold_max: float = 0.135
    pre_click_min: float = 0.080
    pre_click_max: float = 0.300
    post_click_min: float = 0.120
    post_click_max: float = 0.420
    type_delay_min: float = 0.035
    type_delay_max: float = 0.105
    punctuation_pause_min: float = 0.090
    punctuation_pause_max: float = 0.250
    word_pause_min: float = 0.040
    word_pause_max: float = 0.135
    overshoot_probability: float = 0.14
    correction_probability: float = 0.18

    @classmethod
    def named(cls, name: str) -> "HumanActionProfile":
        value = str(name or "balanced").strip().lower()
        if value in {"fast", "quick"}:
            return cls(
                name="fast",
                speed=0.76,
                min_move_steps=10,
                max_move_steps=32,
                click_hold_min=0.035,
                click_hold_max=0.100,
                pre_click_min=0.045,
                pre_click_max=0.170,
                post_click_min=0.070,
                post_click_max=0.250,
                type_delay_min=0.024,
                type_delay_max=0.072,
                punctuation_pause_min=0.060,
                punctuation_pause_max=0.165,
                word_pause_min=0.025,
                word_pause_max=0.085,
                overshoot_probability=0.09,
                correction_probability=0.12,
            )
        if value in {"careful", "slow"}:
            return cls(
                name="careful",
                speed=1.24,
                min_move_steps=18,
                max_move_steps=58,
                click_hold_min=0.060,
                click_hold_max=0.170,
                pre_click_min=0.120,
                pre_click_max=0.430,
                post_click_min=0.180,
                post_click_max=0.620,
                type_delay_min=0.050,
                type_delay_max=0.140,
                punctuation_pause_min=0.135,
                punctuation_pause_max=0.330,
                word_pause_min=0.065,
                word_pause_max=0.185,
                overshoot_probability=0.17,
                correction_probability=0.23,
            )
        return cls()


def persona_for(seed: Any) -> str:
    """Return a stable action profile for an account/profile name."""
    raw = str(seed or "default").encode("utf-8", "ignore")
    value = hashlib.sha256(raw).digest()[0] % 10
    if value <= 1:
        return "careful"
    if value >= 8:
        return "fast"
    return "balanced"


class HumanInteractor:
    """Synchronous Playwright input primitives with reliable fallbacks.

    Movement uses cubic Bézier curves with smooth acceleration/deceleration.
    Clicks target an interior, non-central point. Scrolling uses several wheel
    pulses. Typing uses real keyboard events with word/punctuation pauses.
    """

    def __init__(
        self,
        page: Any,
        profile: Any = "balanced",
        seed: Optional[int] = None,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.page = page
        self.profile = profile if isinstance(profile, HumanActionProfile) else HumanActionProfile.named(str(profile))
        self.rng = random.Random(seed if seed is not None else secrets.randbits(64))
        self.event_sink = event_sink
        self.events: List[Dict[str, Any]] = []
        self._position: Optional[Tuple[float, float]] = None
        self._cursor_overlay_attempted = False
        self._cursor_enabled = str(os.environ.get("SPARKGRID_SHOW_CURSOR", "0") or "0").strip().lower() not in {"", "0", "false", "no", "off"}
        try:
            self._speed_multiplier = max(0.65, min(2.5, float(os.environ.get("SPARKGRID_HUMAN_SPEED_MULTIPLIER", "1.0") or 1.0)))
        except Exception:
            self._speed_multiplier = 1.0
        self._install_cursor_overlay()

    @staticmethod
    def _cursor_overlay_script() -> str:
        return r"""
(() => {
  if (window.__sparkgridHumanCursorInstalled && document.getElementById('__sparkgrid_human_cursor__')) return;
  window.__sparkgridHumanCursorInstalled = true;
  const ensure = () => {
    if (!document.documentElement) return null;
    let dot = document.getElementById('__sparkgrid_human_cursor__');
    if (!dot) {
      dot = document.createElement('div');
      dot.id = '__sparkgrid_human_cursor__';
      dot.setAttribute('aria-hidden', 'true');
      dot.style.cssText = [
        'position:fixed', 'left:0', 'top:0', 'width:25px', 'height:34px',
        'margin-left:-3px', 'margin-top:-3px',
        'background:linear-gradient(145deg,#ffffff 0 18%,#58a6ff 19% 100%)',
        'clip-path:polygon(0 0,0 88%,25% 66%,43% 100%,57% 92%,39% 61%,76% 61%)',
        'filter:drop-shadow(0 0 2px #ffffff) drop-shadow(0 3px 7px rgba(0,0,0,.72))',
        'pointer-events:none', 'z-index:2147483647', 'opacity:1',
        'transform:translate3d(-100px,-100px,0) scale(1)',
        'transition:opacity .08s ease,transform .025s linear',
        'will-change:transform,opacity'
      ].join(';');
      (document.body || document.documentElement).appendChild(dot);
    }
    let ring = document.getElementById('__sparkgrid_human_cursor_ring__');
    if (!ring) {
      ring = document.createElement('div');
      ring.id = '__sparkgrid_human_cursor_ring__';
      ring.setAttribute('aria-hidden', 'true');
      ring.style.cssText = [
        'position:fixed','left:0','top:0','width:30px','height:30px',
        'margin-left:-15px','margin-top:-15px','border-radius:50%',
        'border:2px solid rgba(88,166,255,.80)',
        'box-shadow:0 0 0 4px rgba(88,166,255,.16)',
        'pointer-events:none','z-index:2147483646','opacity:.92',
        'transform:translate3d(-100px,-100px,0) scale(1)',
        'transition:transform .025s linear,opacity .08s ease'
      ].join(';');
      (document.body || document.documentElement).appendChild(ring);
    }
    return {dot, ring};
  };
  const move = (event) => {
    const parts = ensure();
    if (!parts) return;
    parts.dot.style.opacity = '1';
    parts.ring.style.opacity = '.92';
    parts.dot.style.transform = `translate3d(${event.clientX}px,${event.clientY}px,0) scale(1)`;
    parts.ring.style.transform = `translate3d(${event.clientX}px,${event.clientY}px,0) scale(1)`;
  };
  const down = () => {
    const parts = ensure();
    if (!parts) return;
    parts.dot.style.transform += ' scale(.78)';
    parts.ring.style.transform += ' scale(.72)';
  };
  const up = () => {
    const parts = ensure();
    if (!parts) return;
  };
  window.addEventListener('mousemove', move, true);
  window.addEventListener('mousedown', down, true);
  window.addEventListener('mouseup', up, true);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensure, {once:true});
  } else {
    ensure();
  }
})();
"""

    def _install_cursor_overlay(self) -> None:
        if self._cursor_overlay_attempted:
            return
        self._cursor_overlay_attempted = True
        if not self._cursor_enabled:
            return
        script = self._cursor_overlay_script()
        init_ok = False
        live_ok = False
        try:
            self.page.add_init_script(script=script)
            init_ok = True
        except TypeError:
            try:
                self.page.add_init_script(script)
                init_ok = True
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.page.evaluate(script)
            live_ok = True
        except Exception:
            pass
        self._log("cursor_overlay", enabled=True, init_script=init_ok, current_document=live_ok)

    def _ensure_cursor_overlay(self) -> None:
        """Re-create the visible cursor after SPA/document navigation if needed."""
        if not self._cursor_enabled:
            return
        try:
            present = bool(self.page.evaluate(
                "() => !!document.getElementById('__sparkgrid_human_cursor__')"
            ))
        except Exception:
            present = False
        if present:
            return
        try:
            self.page.evaluate(self._cursor_overlay_script())
            self._log("cursor_overlay_restore", current_document=True)
        except Exception as exc:
            self._log("cursor_overlay_restore_error", error=type(exc).__name__)

    def _log(self, kind: str, **payload: Any) -> None:
        event = {"at": time.time(), "kind": kind, "profile": self.profile.name}
        event.update(payload)
        self.events.append(event)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception:
                pass

    def _sleep(self, minimum: float, maximum: float) -> None:
        low = max(0.0, float(minimum))
        high = max(low, float(maximum))
        time.sleep(self.rng.uniform(low, high) * self.profile.speed * self._speed_multiplier)

    def _viewport(self) -> Tuple[float, float]:
        try:
            size = getattr(self.page, "viewport_size", None)
            if size:
                return float(size["width"]), float(size["height"])
        except Exception:
            pass
        try:
            width, height = self.page.evaluate("() => [window.innerWidth, window.innerHeight]")
            return float(width), float(height)
        except Exception:
            return 1280.0, 800.0

    def _clamp(self, x: float, y: float) -> Tuple[float, float]:
        width, height = self._viewport()
        return (
            min(max(2.0, float(x)), max(2.0, width - 2.0)),
            min(max(2.0, float(y)), max(2.0, height - 2.0)),
        )

    def _ensure_position(self) -> Tuple[float, float]:
        if self._position is None:
            width, height = self._viewport()
            self._position = (
                self.rng.uniform(width * 0.24, width * 0.76),
                self.rng.uniform(height * 0.22, height * 0.76),
            )
            try:
                self.page.mouse.move(self._position[0], self._position[1])
            except Exception:
                pass
        return self._position

    @staticmethod
    def _bezier(
        p0: Tuple[float, float],
        p1: Tuple[float, float],
        p2: Tuple[float, float],
        p3: Tuple[float, float],
        t: float,
    ) -> Tuple[float, float]:
        mt = 1.0 - t
        return (
            mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0] + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0],
            mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1] + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1],
        )

    def _path_points(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
    ) -> List[Tuple[float, float]]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = max(1.0, math.hypot(dx, dy))
        nx, ny = -dy / distance, dx / distance
        bend = min(100.0, max(7.0, distance * self.rng.uniform(0.055, 0.17)))
        bend *= self.rng.choice((-1.0, 1.0))
        p1 = (
            start[0] + dx * self.rng.uniform(0.20, 0.38) + nx * bend,
            start[1] + dy * self.rng.uniform(0.20, 0.38) + ny * bend,
        )
        p2 = (
            start[0] + dx * self.rng.uniform(0.62, 0.84) - nx * bend * self.rng.uniform(0.30, 0.85),
            start[1] + dy * self.rng.uniform(0.62, 0.84) - ny * bend * self.rng.uniform(0.30, 0.85),
        )
        steps = int(distance / self.rng.uniform(13.0, 22.0)) + self.rng.randint(5, 10)
        steps = max(self.profile.min_move_steps, min(self.profile.max_move_steps, steps))
        points: List[Tuple[float, float]] = []
        for index in range(1, steps + 1):
            raw = index / float(steps)
            # Smoothstep acceleration/deceleration.
            t = raw * raw * (3.0 - 2.0 * raw)
            x, y = self._bezier(start, p1, p2, end, t)
            jitter = max(0.0, 1.0 - raw) * self.rng.uniform(-0.42, 0.42)
            points.append(self._clamp(x + jitter, y - jitter))
        points[-1] = end
        return points

    def move_to_point(self, x: float, y: float, allow_overshoot: bool = True) -> bool:
        try:
            self._ensure_cursor_overlay()
            start = self._ensure_position()
            end = self._clamp(x, y)
            points: List[Tuple[float, float]]
            if allow_overshoot and self.rng.random() < self.profile.overshoot_probability:
                dx, dy = end[0] - start[0], end[1] - start[1]
                distance = max(1.0, math.hypot(dx, dy))
                amount = min(12.0, max(3.0, distance * self.rng.uniform(0.012, 0.035)))
                overshoot = self._clamp(end[0] + dx / distance * amount, end[1] + dy / distance * amount)
                points = self._path_points(start, overshoot) + self._path_points(overshoot, end)
            else:
                points = self._path_points(start, end)

            per_step = self.rng.uniform(0.0065, 0.0135) * self.profile.speed * self._speed_multiplier
            for px, py in points:
                self.page.mouse.move(px, py)
                time.sleep(per_step)
            self._position = end
            self._log(
                "move",
                start=[round(start[0], 2), round(start[1], 2)],
                end=[round(end[0], 2), round(end[1], 2)],
                distance=round(math.hypot(end[0] - start[0], end[1] - start[1]), 2),
                steps=len(points),
            )
            return True
        except Exception as exc:
            self._log("move_error", error=type(exc).__name__)
            return False

    def _safe_target(self, box: Dict[str, float]) -> Tuple[float, float, float, float]:
        width = max(1.0, float(box.get("width", 1.0)))
        height = max(1.0, float(box.get("height", 1.0)))
        margin_x = min(width * 0.18, max(2.0, width * 0.08))
        margin_y = min(height * 0.22, max(2.0, height * 0.10))
        usable_w = max(1.0, width - margin_x * 2.0)
        usable_h = max(1.0, height - margin_y * 2.0)
        rx = self.rng.betavariate(2.6, 2.6)
        ry = self.rng.betavariate(2.8, 2.8)
        x = float(box.get("x", 0.0)) + margin_x + usable_w * rx
        y = float(box.get("y", 0.0)) + margin_y + usable_h * ry
        x, y = self._clamp(x, y)
        return x, y, rx, ry

    def move_to_locator(self, locator: Any, timeout: int = 5000, allow_overshoot: bool = True) -> Optional[Tuple[float, float]]:
        try:
            locator.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            box = locator.bounding_box(timeout=timeout)
        except TypeError:
            box = locator.bounding_box()
        if not box:
            return None
        x, y, rx, ry = self._safe_target(box)
        if not self.move_to_point(x, y, allow_overshoot=allow_overshoot):
            return None
        self._log("target", x=round(x, 2), y=round(y, 2), ratio_x=round(rx, 3), ratio_y=round(ry, 3))
        return x, y

    def move_to(self, target: Any, y: Optional[float] = None, overshoot: bool = True, timeout: int = 5000) -> bool:
        """Compatibility method: accepts either x/y coordinates or a locator."""
        if y is None and not isinstance(target, (int, float)):
            return self.move_to_locator(target, timeout=timeout, allow_overshoot=overshoot) is not None
        if y is None:
            return False
        return self.move_to_point(float(target), float(y), allow_overshoot=overshoot)

    def hover(self, locator: Any, timeout: int = 5000) -> bool:
        if self.move_to_locator(locator, timeout=timeout) is None:
            try:
                locator.hover(timeout=timeout)
                self._log("hover", method="locator_fallback")
                return True
            except Exception:
                return False
        self._sleep(0.10, 0.58)
        self._log("hover", method="mouse")
        return True

    def click_point(self, x: float, y: float, allow_overshoot: bool = True) -> bool:
        try:
            if not self.move_to_point(x, y, allow_overshoot=allow_overshoot):
                return False
            self._sleep(self.profile.pre_click_min, self.profile.pre_click_max)
            self.page.mouse.down()
            self._sleep(self.profile.click_hold_min, self.profile.click_hold_max)
            self.page.mouse.up()
            self._log("click", x=round(float(x), 2), y=round(float(y), 2), method="mouse_point")
            self._sleep(self.profile.post_click_min, self.profile.post_click_max)
            return True
        except Exception as exc:
            self._log("click_error", method="mouse_point", error=type(exc).__name__)
            return False

    def click(self, locator: Any, timeout: int = 5000) -> bool:
        try:
            target = self.move_to_locator(locator, timeout=timeout)
            if target is None:
                raise RuntimeError("no visible bounding box")
            self._sleep(self.profile.pre_click_min, self.profile.pre_click_max)
            self.page.mouse.down()
            self._sleep(self.profile.click_hold_min, self.profile.click_hold_max)
            self.page.mouse.up()
            self._log("click", x=round(target[0], 2), y=round(target[1], 2), method="mouse")
            self._sleep(self.profile.post_click_min, self.profile.post_click_max)
            return True
        except Exception as exc:
            try:
                locator.click(timeout=timeout)
                self._log("click", method="locator_fallback", error=type(exc).__name__)
                self._sleep(0.08, 0.22)
                return True
            except Exception as fallback_exc:
                self._log("click_error", method="locator_fallback", error=type(fallback_exc).__name__)
                return False

    def press(self, locator: Any, key: str) -> bool:
        try:
            self._sleep(0.04, 0.16)
            locator.press(key)
            self._log("press", key=key)
            self._sleep(0.07, 0.22)
            return True
        except Exception:
            try:
                self.page.keyboard.press(key)
                self._log("press", key=key, method="page_keyboard")
                return True
            except Exception:
                return False

    def type_text(
        self,
        text: Any,
        locator: Any = None,
        clear: bool = False,
        sensitive: bool = False,
        allow_typos: bool = False,
        timeout: int = 5000,
    ) -> bool:
        value = str(text)
        if locator is not None and not self.click(locator, timeout=timeout):
            return False
        if locator is not None and clear:
            modifier = "Meta" if platform.system() == "Darwin" else "Control"
            try:
                locator.press("%s+A" % modifier)
                locator.press("Backspace")
            except Exception:
                try:
                    locator.fill("")
                except Exception:
                    pass

        adjacent = {
            "a": "sqwz", "s": "adwx", "d": "sfec", "f": "dgrv", "g": "fhtb",
            "h": "gjyn", "j": "hkum", "k": "jlio", "l": "kop", "q": "wa",
            "w": "qase", "e": "wsdr", "r": "edft", "t": "rfgy", "y": "tghu",
            "u": "yhji", "i": "ujko", "o": "iklp", "p": "ol", "z": "asx",
            "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
        }
        burst_left = self.rng.randint(2, 6)
        try:
            for char in value:
                if allow_typos and not sensitive and char.lower() in adjacent and self.rng.random() < 0.010:
                    wrong = self.rng.choice(adjacent[char.lower()])
                    self.page.keyboard.type(wrong.upper() if char.isupper() else wrong, delay=0)
                    self._sleep(0.07, 0.20)
                    self.page.keyboard.press("Backspace")
                    self._sleep(0.04, 0.11)

                self.page.keyboard.type(char, delay=0)
                burst_left -= 1
                minimum = self.profile.type_delay_min
                maximum = self.profile.type_delay_max
                if burst_left <= 0:
                    burst_left = self.rng.randint(2, 7)
                    minimum *= 1.35
                    maximum *= 1.85
                elif self.rng.random() < 0.55:
                    minimum *= 0.65
                    maximum *= 0.82

                if char in ".,!?;:\n":
                    self._sleep(self.profile.punctuation_pause_min, self.profile.punctuation_pause_max)
                elif char.isspace():
                    self._sleep(self.profile.word_pause_min, self.profile.word_pause_max)
                else:
                    self._sleep(minimum, maximum)
            self._log("type", length=len(value), sensitive=bool(sensitive), typos=bool(allow_typos and not sensitive))
            return True
        except Exception as exc:
            self._log("type_error", error=type(exc).__name__, length=len(value))
            return False

    def scroll(
        self,
        distance: Optional[int] = None,
        direction: int = 1,
        allow_correction: bool = True,
    ) -> int:
        if distance is None:
            distance = self.rng.randint(420, 920)
        total = max(80, abs(int(distance))) * (1 if direction >= 0 else -1)
        pulses = self.rng.randint(5, 10)
        weights = [math.sin(math.pi * (index + 1) / (pulses + 1)) + 0.20 for index in range(pulses)]
        scale = total / sum(weights)
        sent = 0
        values: List[int] = []
        try:
            self._ensure_cursor_overlay()
            for index, weight in enumerate(weights):
                delta = int(round(weight * scale))
                if index == pulses - 1:
                    delta = total - sent
                sent += delta
                values.append(delta)
                self.page.mouse.wheel(0, delta)
                self._sleep(0.025, 0.085)

            if allow_correction and self.rng.random() < self.profile.correction_probability:
                correction = int(-total * self.rng.uniform(0.035, 0.105))
                self._sleep(0.18, 0.58)
                self.page.mouse.wheel(0, correction)
                values.append(correction)
                sent += correction
            self._log("scroll", requested=total, actual=sent, pulses=values)
            self._sleep(0.16, 0.62)
            return sent
        except Exception as exc:
            self._log("scroll_error", error=type(exc).__name__, requested=total)
            return sent

    def wander(self, moves: int = 1) -> bool:
        width, height = self._viewport()
        ok = False
        for _ in range(max(1, int(moves or 1))):
            x = self.rng.uniform(width * 0.08, width * 0.92)
            y = self.rng.uniform(height * 0.12, height * 0.88)
            ok = self.move_to_point(x, y, allow_overshoot=False) or ok
            self._sleep(0.12, 0.48)
        self._log("wander", moves=max(1, int(moves or 1)))
        return ok

    def dwell(self, minimum: float = 0.8, maximum: float = 2.4, micro_moves: bool = True) -> None:
        duration = self.rng.uniform(float(minimum), float(maximum)) * self.profile.speed
        if not micro_moves:
            time.sleep(duration)
            self._log("idle", duration=round(duration, 3), micro_moves=False)
            return
        start = self._ensure_position()
        count = self.rng.randint(1, 3)
        chunk = duration / float(count + 1)
        for _ in range(count):
            time.sleep(chunk)
            x, y = self._clamp(start[0] + self.rng.uniform(-8.0, 8.0), start[1] + self.rng.uniform(-6.0, 6.0))
            try:
                self.page.mouse.move(x, y)
                self._position = (x, y)
            except Exception:
                pass
        time.sleep(chunk)
        self._log("idle", duration=round(duration, 3), micro_moves=True)

    # Alias used by some older call sites.
    idle = dwell

    def save_trace(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"profile": asdict(self.profile), "events": self.events}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


class Human(HumanInteractor):
    """Backward-compatible wrapper for the previous Human(page, persona=...) API."""

    def __init__(self, page: Any, persona: str = "normal", **kwargs: Any) -> None:
        mapped = {"normal": "balanced", "slow": "careful"}.get(str(persona or "normal").lower(), persona)
        super().__init__(page, profile=mapped, **kwargs)


_CACHE: "weakref.WeakKeyDictionary[Any, HumanInteractor]" = weakref.WeakKeyDictionary()


def make_human(
    page: Any,
    account: Any = None,
    persona: Optional[str] = None,
    event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> HumanInteractor:
    """Return one interactor per live Playwright page.

    A cached instance preserves the last pointer position across helper calls.
    """
    try:
        existing = _CACHE.get(page)
    except Exception:
        existing = None
    if existing is not None:
        if event_sink is not None:
            existing.event_sink = event_sink
        return existing

    profile_name = persona or str(os.environ.get("SPARKGRID_HUMAN_PERSONA", "") or "").strip() or persona_for(account)
    seed_raw = hashlib.sha256(str(account or "default").encode("utf-8", "ignore")).digest()[:8]
    seed = int.from_bytes(seed_raw, "big") ^ secrets.randbits(32)
    human = HumanInteractor(page, HumanActionProfile.named(profile_name), seed=seed, event_sink=event_sink)
    try:
        _CACHE[page] = human
    except Exception:
        pass
    return human
