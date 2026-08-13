"""Reactive browser-context page ownership for SparkGrid workflows.

The router never treats the newest page as the operation page. Ownership moves
only to a page whose fresh DOM confirms the active Instagram composer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

_ROUTERS: Dict[int, "BrowserPageRouter"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _path(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
        return parsed.path or (parsed.scheme + ":" if parsed.scheme else "")
    except Exception:
        return ""


def inspect_page(page) -> Dict[str, Any]:
    """Read a fresh, secret-free page classification."""
    url = str(getattr(page, "url", "") or "")
    if url == "about:blank":
        return {"kind": "blank", "url_path": "about:blank", "title": "", "composer": False}
    try:
        observed = page.evaluate(
            """() => { // SPARKGRID_PAGE_ROUTER_INSPECT
              const visible = el => {
                if (!el) return false;
                const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                return r.width>8 && r.height>8 && s.display!=='none' &&
                  s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>.01;
              };
              const roots=[...document.querySelectorAll("[role='dialog'],[aria-modal='true']")].filter(visible);
              const labels=[...document.querySelectorAll('button,[role="button"]')].filter(visible)
                .map(el=>String(el.getAttribute('aria-label')||el.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase());
              const composer=roots.some(root => {
                const text=String(root.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                const media=!!root.querySelector("video,canvas,input[type='file'],[contenteditable='true'],textarea");
                const action=labels.some(x=>/^(next|share|post|publish|continue)$/.test(x));
                return action && (media || /crop|cover photo|trim|write a caption|create new post|edit/.test(text));
              });
              const consent=/\\/consent\\/?/.test(location.pathname);
              return {composer, consent, title:String(document.title||'').slice(0,160),
                focused:document.hasFocus(), visible:document.visibilityState==='visible'};
            }"""
        )
    except Exception:
        observed = {}
    if not isinstance(observed, dict):
        observed = {}
    host = (urlparse(url).hostname or "").lower()
    kind = "other"
    if observed.get("composer"):
        kind = "instagram_composer"
    elif observed.get("consent") or "/consent" in _path(url):
        kind = "instagram_consent"
    elif host == "instagram.com" or host.endswith(".instagram.com"):
        kind = "instagram"
    return {
        "kind": kind,
        "url_path": _path(url),
        "title": str(observed.get("title") or "")[:160],
        "composer": bool(observed.get("composer")),
        "focused": bool(observed.get("focused")),
        "visible": bool(observed.get("visible")),
    }


class BrowserPageRouter:
    def __init__(self, context, primary_page):
        self.context = context
        self.primary_page = primary_page
        self.operation_page = primary_page
        self.records: Dict[int, Dict[str, Any]] = {}
        self._sequence = 0
        for page in list(getattr(context, "pages", []) or []):
            self._register(page, opener=None)
        if id(primary_page) not in self.records:
            self._register(primary_page, opener=None)
        try:
            context.on("page", self._on_page)
        except Exception:
            pass

    def _register(self, page, opener=None) -> None:
        key = id(page)
        if key in self.records:
            return
        self._sequence += 1
        opener_id = self.records.get(id(opener), {}).get("page_id", "") if opener else ""
        self.records[key] = {
            "page": page,
            "page_id": f"page-{self._sequence}",
            "created_at": _now(),
            "opener": opener_id,
            "closed_at": "",
            "navigations": [],
        }
        try:
            page.on("close", lambda: self._mark_closed(page))
        except Exception:
            pass
        try:
            page.on("framenavigated", lambda frame: self._mark_navigation(page, frame))
        except Exception:
            pass

    def _on_page(self, page) -> None:
        opener = None
        try:
            opener = page.opener()
        except Exception:
            pass
        self._register(page, opener=opener)

    def _mark_closed(self, page) -> None:
        record = self.records.get(id(page))
        if record and not record["closed_at"]:
            record["closed_at"] = _now()

    def _mark_navigation(self, page, frame) -> None:
        try:
            if frame is not page.main_frame:
                return
        except Exception:
            pass
        record = self.records.get(id(page))
        if record is not None:
            record["navigations"].append({"at": _now(), "url_path": _path(getattr(page, "url", ""))})

    @staticmethod
    def _open(page) -> bool:
        try:
            return not bool(page.is_closed())
        except Exception:
            return True

    def refresh(self) -> List[Dict[str, Any]]:
        for page in list(getattr(self.context, "pages", []) or []):
            self._register(page)
        result = []
        for record in self.records.values():
            page = record["page"]
            if not self._open(page):
                self._mark_closed(page)
            state = inspect_page(page) if self._open(page) else {
                "kind": "closed", "url_path": "", "title": "", "composer": False,
                "focused": False, "visible": False,
            }
            record.update(state)
            result.append({k: v for k, v in record.items() if k != "page"})
        return result

    def select_operation_page(self, require_composer: bool = False):
        self.refresh()
        current = self.operation_page
        if self._open(current):
            current_state = self.records[id(current)]
            if not require_composer or current_state.get("composer"):
                return current
        composers = [
            record["page"] for record in self.records.values()
            if self._open(record["page"]) and record.get("composer")
        ]
        if len(composers) == 1:
            self.operation_page = composers[0]
            return composers[0]
        if not require_composer and self._open(self.primary_page):
            self.operation_page = self.primary_page
            return self.primary_page
        return current

    def handle_auxiliary_pages(self, consent_handler: Optional[Callable[[Any], bool]] = None) -> None:
        self.refresh()
        for record in self.records.values():
            page = record["page"]
            if page is self.operation_page or not self._open(page):
                continue
            kind = record.get("kind")
            if kind == "instagram_consent" and consent_handler is not None:
                consent_handler(page)
            elif kind in {"blank", "other"}:
                try:
                    page.close()
                except Exception:
                    pass
        self.refresh()

    def capture_all(self, dump, label: str) -> None:
        self.refresh()
        for record in self.records.values():
            page = record["page"]
            if not self._open(page):
                continue
            page_label = f"{label}_{record['page_id']}_{record.get('kind', 'unknown')}"
            dump.capture(page, page_label, "context page snapshot", force_snapshot=True)
            capture_dom = getattr(dump, "capture_safe_dom", None)
            if callable(capture_dom):
                capture_dom(page, page_label)


def attach_page_router(context, primary_page) -> BrowserPageRouter:
    existing = getattr(context, "_sparkgrid_page_router", None) or _ROUTERS.get(id(context))
    if isinstance(existing, BrowserPageRouter):
        return existing
    router = BrowserPageRouter(context, primary_page)
    _ROUTERS[id(context)] = router
    try:
        setattr(context, "_sparkgrid_page_router", router)
    except Exception:
        pass
    return router


def router_for_page(page) -> Optional[BrowserPageRouter]:
    try:
        context = page.context
        router = getattr(context, "_sparkgrid_page_router", None) or _ROUTERS.get(id(context))
    except Exception:
        return None
    return router if isinstance(router, BrowserPageRouter) else None
