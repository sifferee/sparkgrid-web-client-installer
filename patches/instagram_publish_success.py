#!/usr/bin/env python3
"""Latch fresh, visible Instagram publish-success UI without retaining page text."""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional


_INSTALL_SCRIPT = r"""() => { // IG_PUBLISH_SUCCESS_INSTALL
  const key = '__sparkgridPublishSuccess';
  const state = window[key] || {latched: null, observer: null};
  if (state.observer) state.observer.disconnect();
  state.latched = null;
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!(r.width > 2 && r.height > 2 && s.display !== 'none' &&
          s.visibility !== 'hidden' && Number.parseFloat(s.opacity || '1') > 0.01 &&
          r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth))
      return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' || style.visibility === 'hidden' ||
          Number.parseFloat(style.opacity || '1') <= 0.01) return false;
    }
    return true;
  };
  const signalCode = text => {
    if (/\bshared successfully\b/i.test(text)) return 'shared_successfully';
    if (/\byour reel (?:was|has been) shared\b/i.test(text)) return 'your_reel_shared';
    if (/\breel shared\b/i.test(text)) return 'reel_shared';
    return '';
  };
  const baselineNodes = new WeakSet();
  const candidates = () => {
    const selectors = [
      "[role='dialog']", "[role='status']", "[role='alert']", "[aria-live]",
      "h1", "h2", "h3", "[role='heading']", "body *"
    ];
    const seen = new Set();
    const matches = [];
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (seen.has(el) || !visible(el)) continue;
        seen.add(el);
        const text = normalize(el.innerText || el.textContent);
        const code = signalCode(text);
        if (!code) continue;
        const dialog = el.closest("[role='dialog'],[aria-modal='true']");
        const semanticRole = el.getAttribute('role') || '';
        const ariaLive = String(el.getAttribute('aria-live') || '');
        // Feed text and other background content are never publication
        // evidence.  Accept only an owned modal or an explicit live surface.
        if (!dialog && !['status', 'alert'].includes(semanticRole) && !ariaLive) continue;
        const rect = el.getBoundingClientRect();
        matches.push({el, dialog, code, area: rect.width * rect.height, text});
      }
    }
    matches.sort((a, b) => a.area - b.area);
    return matches;
  };
  for (const match of candidates()) baselineNodes.add(match.el);
  const inspect = () => {
    const found = candidates().find(match => !baselineNodes.has(match.el));
    if (!found) return null;
    const el = found.el;
    const dialog = found.dialog;
    const semantic = el.getAttribute('role') || (dialog ? 'dialog' : '') ||
      (el.getAttribute('aria-live') ? 'aria-live' : '') ||
      (/^H[1-6]$/.test(el.tagName) ? 'heading' : 'visible_text');
    return {
      code: found.code,
      semantic_role: semantic,
      in_dialog: !!dialog,
      aria_live: String(el.getAttribute('aria-live') || ''),
      visible: true
    };
  };
  state.inspect = inspect;
  state.observer = new MutationObserver(() => {
    if (!state.latched) state.latched = inspect();
  });
  state.observer.observe(document.documentElement, {
    subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['hidden', 'aria-hidden', 'aria-live', 'role', 'style', 'class']
  });
  window[key] = state;
  return true;
}"""

_SNAPSHOT_SCRIPT = r"""() => { // IG_PUBLISH_SUCCESS_SNAPSHOT
  const state = window.__sparkgridPublishSuccess;
  if (!state) return {matched: false};
  if (!state.latched && state.inspect) state.latched = state.inspect();
  return state.latched ? Object.assign({matched: true}, state.latched) : {matched: false};
}"""

_DONE_SEARCH_SCRIPT = r"""() => { // IG_PUBLISH_SUCCESS_DONE_SEARCH
  document.querySelectorAll("[data-sparkgrid-success-done='1']")
    .forEach(el => el.removeAttribute('data-sparkgrid-success-done'));
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const success = text => /\b(shared successfully|reel shared|your reel (?:was|has been) shared)\b/i.test(text);
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!(r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' &&
      Number.parseFloat(s.opacity || '1') > 0.01 && r.bottom > 0 && r.right > 0 &&
      r.top < innerHeight && r.left < innerWidth)) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' || style.visibility === 'hidden' ||
          Number.parseFloat(style.opacity || '1') <= 0.01) return false;
    }
    return true;
  };
  const owned = /(^|\.)instagram\.com$/i.test(location.hostname);
  window.__sparkgridSafePageId = window.__sparkgridSafePageId ||
    `page-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`;
  const dialogs = [...document.querySelectorAll("[role='dialog'],[aria-modal='true']")]
    .filter(el => visible(el) && success(normalize(el.innerText || el.textContent)));
  const dialog = dialogs[dialogs.length - 1];
  const candidates = dialog ? [...dialog.querySelectorAll("button,[role='button'],[aria-label]")]
    .filter(el => normalize(el.getAttribute('aria-label') || el.innerText || el.textContent) === 'done')
    .map(el => ({
      el,
      tag: String(el.tagName || '').toLowerCase(),
      role: String(el.getAttribute('role') || ''),
      text: normalize(el.innerText || el.textContent),
      aria_label: normalize(el.getAttribute('aria-label') || ''),
      visible: visible(el),
      enabled: !(el.disabled || el.getAttribute('aria-disabled') === 'true')
    })) : [];
  const chosen = owned ? candidates.find(item => item.visible && item.enabled) : null;
  if (chosen) chosen.el.setAttribute('data-sparkgrid-success-done', '1');
  return {
    page_id: window.__sparkgridSafePageId,
    owned, dialog_present: !!dialog, candidate_count: candidates.length,
    candidates: candidates.map(({el, ...safe}) => safe),
    selected: !!chosen
  };
}"""

_DONE_STATE_SCRIPT = r"""() => { // IG_PUBLISH_SUCCESS_DONE_STATE
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = el => {
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.display !== 'none' &&
      s.visibility !== 'hidden' && Number.parseFloat(s.opacity || '1') > 0.01 &&
      r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  const success = text => /\b(shared successfully|reel shared|your reel (?:was|has been) shared)\b/i.test(text);
  const dialog_present = [...document.querySelectorAll("[role='dialog'],[aria-modal='true']")]
    .some(el => visible(el) && success(normalize(el.innerText || el.textContent)));
  const working_page = !![...document.querySelectorAll("main[role='main'],main")]
    .find(visible);
  return {dialog_present, working_page, ready: !dialog_present && working_page};
}"""

_CLOSE_SCRIPT = r"""() => {
  const state = window.__sparkgridPublishSuccess;
  if (state && state.observer) state.observer.disconnect();
  delete window.__sparkgridPublishSuccess;
}"""


class PublishSuccessObserver:
    """Observe only UI success that appears after this observer is installed."""

    def __init__(self, page: Any):
        self.page = page
        self.started = False
        try:
            self.started = bool(page.evaluate(_INSTALL_SCRIPT))
        except Exception:
            self.started = False

    def snapshot(self) -> Dict[str, Any]:
        if not self.started:
            return {"matched": False}
        try:
            value = self.page.evaluate(_SNAPSHOT_SCRIPT)
            if isinstance(value, dict) and value.get("matched"):
                return {
                    "matched": True,
                    "code": str(value.get("code") or ""),
                    "semantic_role": str(value.get("semantic_role") or ""),
                    "in_dialog": bool(value.get("in_dialog")),
                    "aria_live": str(value.get("aria_live") or ""),
                    "visible": bool(value.get("visible")),
                }
        except Exception:
            pass
        return {"matched": False}

    def close(self) -> None:
        if not self.started:
            return
        try:
            self.page.evaluate(_CLOSE_SCRIPT)
        except Exception:
            pass
        self.started = False


def cleanup_success_dialog(
    page: Any,
    *,
    emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    wait_seconds: float = 4.0,
) -> Dict[str, Any]:
    """Best-effort semantic Done cleanup after durable publication acceptance."""
    def report(event: str, payload: Dict[str, Any]) -> None:
        if emit is not None:
            try:
                emit(event, payload)
            except Exception:
                pass

    try:
        search = page.evaluate(_DONE_SEARCH_SCRIPT)
        if not isinstance(search, dict):
            search = {}
    except Exception:
        search = {}
    safe_search = {
        "page_id": str(search.get("page_id") or ""),
        "owned": bool(search.get("owned")),
        "dialog_present": bool(search.get("dialog_present")),
        "candidate_count": int(search.get("candidate_count") or 0),
    }
    report("upload_success_done_search", safe_search)
    for candidate in list(search.get("candidates") or []):
        if not isinstance(candidate, dict):
            continue
        report("upload_success_done_candidate", {
            **safe_search,
            "tag": str(candidate.get("tag") or ""),
            "role": str(candidate.get("role") or ""),
            "text": str(candidate.get("text") or ""),
            "aria_label": str(candidate.get("aria_label") or ""),
            "visible": bool(candidate.get("visible")),
            "enabled": bool(candidate.get("enabled")),
        })
    if not search.get("selected"):
        if not search.get("dialog_present"):
            result = {
                **safe_search, "clicked": False, "closed": True, "ready": True,
                "status": "surface_already_absent",
            }
            report("upload_success_done_dialog_closed", result)
            return result
        result = {
            **safe_search, "clicked": False, "closed": False, "ready": False,
            "status": "done_unavailable",
        }
        report("upload_success_done_cleanup_failed", result)
        return result

    locator = page.locator("[data-sparkgrid-success-done='1']")
    try:
        locator_count = int(locator.count())
    except Exception:
        locator_count = 0
    started = {**safe_search, "locator": "data-sparkgrid-success-done", "locator_count": locator_count}
    report("upload_success_done_click_started", started)
    clicked = False
    try:
        if locator_count == 1:
            locator.click(timeout=3000)
            clicked = True
    except Exception:
        clicked = False
    report("upload_success_done_click_finished", {**started, "clicked": clicked})

    deadline = time.time() + max(0.0, float(wait_seconds))
    state: Dict[str, Any] = {}
    while True:
        try:
            value = page.evaluate(_DONE_STATE_SCRIPT)
            state = value if isinstance(value, dict) else {}
        except Exception:
            state = {}
        if (
            not state.get("dialog_present")
            or state.get("ready")
            or time.time() >= deadline
        ):
            break
        time.sleep(0.2)
    closed = not bool(state.get("dialog_present"))
    ready = bool(state.get("ready") or closed)
    result = {
        **safe_search, "clicked": clicked, "closed": closed, "ready": ready,
        "status": "dialog_closed" if closed else "dialog_still_open",
    }
    report(
        "upload_success_done_dialog_closed" if closed else "upload_success_done_cleanup_failed",
        result,
    )
    return result
