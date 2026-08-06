"""Shared, secret-free Instagram blocking-dialog classification and handling.

Only stable category codes cross this boundary.  In particular, no dialog text,
HTML, credentials, cookies, or endpoint data is returned to callers.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from blocking_popup_transaction import (
    inspect_topmost_blocker,
    perform_fresh_action,
)
try:
    from ig_human import make_human
except Exception:  # pragma: no cover - optional in narrow test runtimes
    make_human = None


BLOCKING_NOT_DISMISSED = "blocking_dialog_not_dismissed"
NO_BLOCKER = "NO_BLOCKER"
HANDLED_REEVALUATE = "HANDLED_REEVALUATE"
TRANSITIONING_RETRY = "TRANSITIONING_RETRY"
UNKNOWN_BLOCKER = "UNKNOWN_BLOCKER"
TERMINAL_MANUAL = "TERMINAL_MANUAL"


def _dialog_human(page: Any) -> Any:
    if make_human is None:
        return None
    try:
        return make_human(page)
    except Exception:
        return None


def _inspect(page) -> Dict[str, Any]:
    """Return only the topmost dialog's normalized category."""
    if hasattr(page, "frames"):
        structural = inspect_topmost_blocker(page)
        if structural.get("document_category") == "browser_internal_error":
            return {
                "category": "",
                "present": False,
                "progress": False,
                "fingerprint": "",
                "document_category": "browser_internal_error",
            }
        if structural.get("present"):
            category = {
                "save_login_info": "save_login",
                "notifications_prompt": "notification",
                "unknown_blocker": "unknown_dialog",
                "promo_or_ad": "unknown_dialog",
            }.get(
                str(structural.get("category") or ""),
                str(structural.get("category") or ""),
            )
            return {
                "category": category,
                "present": True,
                "progress": category == "operation_processing",
                "fingerprint": str(structural.get("fingerprint") or ""),
                **(
                    {"recommended_action": str(
                        structural.get("recommended_action") or ""
                    )}
                    if category == "open_in_app" else {}
                ),
                "document_epoch": str(structural.get("document_epoch") or ""),
                "mutation_epoch": int(structural.get("mutation_epoch") or 0),
                "frame_ref": str(structural.get("frame_ref") or ""),
            }
    try:
        result = page.evaluate(
            """() => { // IG_DIALOG_GATE_INSPECT
              const visible = (el) => {
                 const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                 if (!(r.width > 8 && r.height > 8 && s.display !== 'none' &&
                   s.visibility !== 'hidden' && s.opacity !== '0' && r.bottom > 0 &&
                  r.right > 0 && r.top < innerHeight && r.left < innerWidth)) return false;
                 for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
                   const style = getComputedStyle(node);
                   if (node.hidden || node.inert || node.getAttribute('aria-hidden') === 'true' ||
                       style.display === 'none' || style.visibility === 'hidden' ||
                       Number.parseFloat(style.opacity || '1') <= 0.01) return false;
                 }
                 return true;
              };
              const score = (el, i) => {
                const z = Number.parseInt(getComputedStyle(el).zIndex, 10);
                return (Number.isFinite(z) ? z : 0) * 100000 + i;
              };
              const dialogs = [...document.querySelectorAll("[role='dialog'],[aria-modal='true']")]
                .filter(visible).map((el, i) => ({el, score: score(el, i)}))
                .sort((a, b) => b.score - a.score);
              const top = dialogs[0] && dialogs[0].el;
              if (!top) return {category:'', present:false};
              const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const text = normalize(top.innerText || top.textContent);
              const labels = [...top.querySelectorAll('button,[role="button"],[aria-label]')]
                .filter(visible).map(el => normalize(el.getAttribute('aria-label') || el.innerText || el.textContent));
              const progress = !!top.querySelector(
                "[role='progressbar'],svg[aria-label='Loading...'],[aria-busy='true']");
              const signature = [
                normalize(top.getAttribute('role')),
                normalize(top.getAttribute('aria-label')),
                text,
                labels.join('|'),
                progress ? 'progress' : '',
              ].join('|');
              let fingerprint = 2166136261;
              for (let i = 0; i < signature.length; i++) {
                fingerprint ^= signature.charCodeAt(i);
                fingerprint = Math.imul(fingerprint, 16777619);
              }
              const result = (category) => ({
                category, present:true, progress,
                fingerprint:(fingerprint >>> 0).toString(16),
              });
              const composerAction = labels.some(label =>
                /^(next|share|post|publish|continue|done|edit)$/.test(label));
              const composerMedia = !!top.querySelector(
                "video,canvas,input[type='file'],[contenteditable='true'],textarea");
              if (composerAction && (composerMedia ||
                  /crop|cover photo|trim|write a caption|create new post|edit/.test(text)))
                return result('operation_composer');
              // Instagram keeps the composer-shaped modal mounted after Share
              // and replaces its contents with a spinner headed "Sharing".
              // This is an in-flight operation, not an unknown account blocker.
              // This JavaScript lives in a normal Python string.  Escape the
              // backslashes so the browser receives regex word boundaries,
              // not Python's U+0008 backspace character.
              if (progress || /\\b(sharing|posting|publishing|processing|uploading|preparing|checking)\\b/.test(text))
                return result('operation_processing');
              if (/\\b(shared successfully|reel shared|your reel (?:was|has been) shared)\\b/.test(text))
                return result('operation_success');
              const cookieHeading = text.includes('allow the use of cookies from instagram on this browser?');
              if (cookieHeading && labels.includes('allow all cookies') && labels.includes('decline optional cookies'))
                return result('cookie_consent');
              if (text.includes('save your login info') && labels.includes('not now'))
                return result('save_login');
              if (text.includes('what happened') && text.includes('we removed your post') && text.includes('see why'))
                return result('policy_notice');
              if (text.includes('turn on notifications') && text.includes('not now'))
                return result('notification');
              if (/(challenge|checkpoint|confirm it'?s you|help us confirm|verification)/.test(text))
                return result('checkpoint');
              if (/(try again later|we restrict|restricted|suspicious)/.test(text))
                return result('restriction');
              if (/(suspended|disabled)/.test(text)) return result('suspended');
              return result('unknown_dialog');
            }"""
        )
        if isinstance(result, dict):
            return {
                "category": str(result.get("category") or ""),
                "present": bool(result.get("present")),
                "progress": bool(result.get("progress")),
                # Only a one-way structural hash crosses the browser boundary.
                "fingerprint": str(result.get("fingerprint") or ""),
            }
    except Exception:
        pass
    return {"category": "", "present": False}


def inspect_dialog(page) -> Dict[str, Any]:
    """Public dialog inspection with no raw account-sensitive content."""
    return _inspect(page)


def dismiss_known_dialog_once(page, category: str) -> bool:
    """Dispatch exactly one safe action for the currently visible known dialog.

    Observation/retry ownership belongs to ``BrowserGoalEngine``.  This adapter
    deliberately performs no internal polling and never clicks an unknown
    dialog.
    """
    action = {
        "cookie_consent": "decline_optional_cookies",
        "save_login": "not_now",
        "notification": "not_now",
        "policy_notice": "close",
    }.get(str(category or ""))
    return bool(action and _semantic_action(page, action))


def _document_observation(page) -> Dict[str, Any]:
    """Read navigation/DOM liveness without retaining page text or URLs."""
    try:
        result = page.evaluate(
            """() => { // IG_DIALOG_CONTINUATION_OBSERVE
              const visible = (el) => {
                const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                return r.width>8 && r.height>8 && s.display!=='none' &&
                  s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>0.01 &&
                  r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
              };
              const normalized = String((document.body && document.body.innerText) || '')
                .replace(/\\s+/g, ' ').trim();
              const busy = [...document.querySelectorAll(
                "[aria-busy='true'],[role='progressbar'],svg[aria-label='Loading...']"
              )].some(visible);
              const password = [...document.querySelectorAll(
                "input[type='password'],input[name='password'],input[autocomplete='current-password']"
              )].some(visible);
              const authenticated = [...document.querySelectorAll(
                "a[href*='/direct/inbox'],a[href*='/accounts/edit'],svg[aria-label='Home'],svg[aria-label='New post']"
              )].some(visible);
              const signature = [
                location.href, document.readyState, normalized.length,
                document.body ? document.body.childElementCount : 0,
                document.querySelectorAll("[role='dialog'],[aria-modal='true']").length,
                password ? 'login' : '', authenticated ? 'authenticated' : '',
              ].join('|');
              let hash=2166136261;
              for(let i=0;i<signature.length;i++){hash^=signature.charCodeAt(i);hash=Math.imul(hash,16777619);}
              return {
                loading: document.readyState !== 'complete' || busy || normalized.length === 0,
                authenticated_ui: authenticated,
                login_ui: password,
                document_fingerprint: (hash>>>0).toString(16),
              };
            }"""
        )
        if isinstance(result, dict):
            return {
                "loading": bool(result.get("loading")),
                "authenticated_ui": bool(result.get("authenticated_ui")),
                "login_ui": bool(result.get("login_ui")),
                "document_fingerprint": str(result.get("document_fingerprint") or ""),
            }
    except Exception:
        pass
    return {
        "loading": False,
        "authenticated_ui": False,
        "login_ui": False,
        "document_fingerprint": "",
    }


def _observe(page) -> Dict[str, Any]:
    observed = _inspect(page)
    observed.update(_document_observation(page))
    try:
        observed["url"] = str(getattr(page, "url", "") or "")
    except Exception:
        observed["url"] = ""
    return observed


def _semantic_action(page, action: str) -> bool:
    """Click a named action inside the topmost dialog; never use coordinates."""
    if action not in {"allow_all_cookies", "decline_optional_cookies", "not_now", "close"}:
        return False
    wanted = {
        "allow_all_cookies": "allow all cookies",
        "decline_optional_cookies": "decline optional cookies",
        "not_now": "not now",
        "close": "close",
    }[action]
    if hasattr(page, "frames"):
        observed = inspect_topmost_blocker(page)
        structural_action = {
            "allow_all_cookies": "cookie_allow_all",
            "decline_optional_cookies": "cookie_decline_optional",
            "not_now": "dismiss_not_now",
            "close": "dismiss_close",
        }.get(action, "")
        if structural_action:
            result = perform_fresh_action(
                page,
                observed,
                structural_action,
                human=_dialog_human(page),
            )
            return bool(result.get("ok"))
    try:
        return bool(page.evaluate(
            f"""() => {{ // IG_DIALOG_GATE_ACTION {action}
              const visible = (el) => {{ const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                return r.width>8 && r.height>8 && s.display!=='none' && s.visibility!=='hidden' &&
                  s.opacity!=='0' && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth; }};
              const dialogs=[...document.querySelectorAll("[role='dialog'],[aria-modal='true']")].filter(visible)
                .map((el,i)=>{{const z=Number.parseInt(getComputedStyle(el).zIndex,10);return {{el,score:(Number.isFinite(z)?z:0)*100000+i}};}})
                .sort((a,b)=>b.score-a.score);
              const top=dialogs[0] && dialogs[0].el; if(!top) return false;
              const label=(el)=>String(el.getAttribute('aria-label')||el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim().toLowerCase();
              const wanted={wanted!r};
              const target=[...top.querySelectorAll('button,[role="button"],[aria-label]')].find(el=>visible(el) && label(el)===wanted);
              if(!target) return false;
              target.click(); return true;
            }}"""
        ))
    except Exception:
        return False


def _wait_absent(page, category: str, deadline: float) -> bool:
    while time.time() < deadline:
        seen = _inspect(page)
        if not seen["present"] or seen["category"] != category:
            return True
        time.sleep(0.2)
    seen = _inspect(page)
    return not seen["present"] or seen["category"] != category


def continue_after_dialog(
    page,
    *,
    allow_safe_close: bool = False,
    wait_seconds: float = 4.0,
    cookie_action: str = "decline_optional_cookies",
) -> Dict[str, Any]:
    """Apply the shared bounded contract before sensitive browser actions.

    KNOWN_DIALOG -> one click -> HANDLED_REEVALUATE -> fresh URL/DOM reads ->
    bounded transition observation -> next actual state.  Unknown is terminal
    only after three identical, fresh, non-loading observations with no DOM or
    navigation change.  No pre-click classification survives a successful
    click.
    """
    deadline = time.time() + max(0.0, float(wait_seconds))
    observed = _observe(page)
    while not observed["present"] and time.time() < deadline:
        time.sleep(0.2)
        observed = _observe(page)

    handled = False
    clicked_at = 0.0
    clicked_action = ""
    clicked_category = ""
    post_click_read_pending = False
    clicked_fingerprints: set[tuple[str, str]] = set()
    fresh_reads = 0
    stable_reads = 0
    settled_reads = 0
    last_identity: tuple[str, str, str, str] | None = None
    last_document = ""
    while True:
        fresh_reads += 1
        category = str(observed.get("category") or "")
        present = bool(observed.get("present"))
        loading = bool(observed.get("loading") or observed.get("progress"))

        if not present:
            if not handled:
                return {
                    "outcome": NO_BLOCKER, "state": "", "present": False,
                    "dismissed": False, "fresh_reads": fresh_reads,
                    "stable_reads": 0,
                }
            # Require two quiet post-click reads. A blank/loading React frame
            # never qualifies as the next actual state.
            document = str(observed.get("document_fingerprint") or "")
            if loading:
                settled_reads = 0
            elif document and document == last_document:
                settled_reads += 1
            else:
                settled_reads = 1
            last_document = document
            if loading and time.time() >= deadline:
                return {
                    "outcome": TRANSITIONING_RETRY, "state": "",
                    "present": False, "dismissed": True,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }
            if settled_reads >= 2 or (not loading and time.time() >= deadline):
                return {
                    "outcome": HANDLED_REEVALUATE, "state": "",
                    "present": False, "dismissed": True,
                    "fresh_reads": fresh_reads, "stable_reads": settled_reads,
                    "clicked_at": clicked_at, "clicked_action": clicked_action,
                    "clicked_category": clicked_category,
                }
        elif category in {"operation_composer", "operation_success"}:
            return {
                "outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                "state": "", "present": True, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": 1,
            }
        elif category == "operation_processing" or loading:
            stable_reads = settled_reads = 0
            last_identity = None
            if time.time() >= deadline:
                return {
                    "outcome": TRANSITIONING_RETRY,
                    "state": "",
                    "present": present,
                    "dismissed": handled,
                    "fresh_reads": fresh_reads,
                    "stable_reads": 0,
                }
        elif (
            handled
            and clicked_category == "open_in_app"
            and category != clicked_category
        ):
            return {
                "outcome": HANDLED_REEVALUATE, "state": "",
                "present": True, "dismissed": True,
                "fresh_reads": fresh_reads, "stable_reads": 1,
                "clicked_at": clicked_at, "clicked_action": clicked_action,
                "clicked_category": clicked_category,
            }
        elif category == "open_in_app":
            action = str(observed.get("recommended_action") or "")
            if handled or action not in {"dismiss_not_now", "dismiss_cancel"}:
                return {
                    "outcome": TERMINAL_MANUAL,
                    "state": BLOCKING_NOT_DISMISSED,
                    "present": True, "dismissed": False,
                    "fresh_reads": fresh_reads, "stable_reads": 1,
                }
            fingerprint = str(observed.get("fingerprint") or category)
            click_key = (category, fingerprint)
            if click_key in clicked_fingerprints:
                return {
                    "outcome": TERMINAL_MANUAL,
                    "state": BLOCKING_NOT_DISMISSED,
                    "present": True, "dismissed": False,
                    "fresh_reads": fresh_reads, "stable_reads": stable_reads,
                }
            clicked_fingerprints.add(click_key)
            dispatched = perform_fresh_action(
                page, observed, action, human=_dialog_human(page)
            )
            if not dispatched.get("ok"):
                return {
                    "outcome": TERMINAL_MANUAL,
                    "state": BLOCKING_NOT_DISMISSED,
                    "present": True, "dismissed": False,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }
            handled = True
            clicked_at = time.time()
            clicked_action = action
            clicked_category = category
            deadline = max(deadline, clicked_at + 0.05)
            post_click_read_pending = True
            stable_reads = settled_reads = 0
            last_identity = None
            last_document = ""
        elif category in {"policy_notice", "cookie_consent", "notification", "save_login"}:
            action = {
                "policy_notice": "close",
                "cookie_consent": cookie_action,
                "notification": "not_now",
                "save_login": "not_now",
            }[category]
            allowed = category != "policy_notice" or allow_safe_close
            fingerprint = str(observed.get("fingerprint") or category)
            click_key = (category, fingerprint)
            if not allowed or click_key in clicked_fingerprints:
                if time.time() >= deadline or not allowed:
                    return {
                        "outcome": TERMINAL_MANUAL,
                        "state": BLOCKING_NOT_DISMISSED,
                        "present": True, "dismissed": False,
                        "fresh_reads": fresh_reads, "stable_reads": stable_reads,
                    }
            else:
                clicked_fingerprints.add(click_key)
                if not _semantic_action(page, action):
                    return {
                        "outcome": TERMINAL_MANUAL,
                        "state": BLOCKING_NOT_DISMISSED,
                        "present": True, "dismissed": False,
                        "fresh_reads": fresh_reads, "stable_reads": 0,
                    }
                handled = True
                clicked_at = time.time()
                clicked_action = action
                clicked_category = category
                # The caller's discovery budget may be nearly exhausted.
                # Reserve a small, bounded post-click epoch so the known
                # classification can never become terminal by fall-through.
                deadline = max(deadline, clicked_at + 0.05)
                post_click_read_pending = True
                # The pre-click category/fingerprint/DOM epoch is discarded.
                stable_reads = settled_reads = 0
                last_identity = None
                last_document = ""
        elif category in {"checkpoint", "restriction", "suspended"}:
            return {
                "outcome": TERMINAL_MANUAL,
                "state": {
                    "checkpoint": "checkpoint",
                    "restriction": "restricted",
                    "suspended": "suspended",
                }[category],
                "present": True, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": 1,
            }
        else:
            # An authenticated surface wins over a stray dialog-shaped node.
            if observed.get("authenticated_ui"):
                return {
                    "outcome": HANDLED_REEVALUATE if handled else NO_BLOCKER,
                    "state": "", "present": True, "dismissed": handled,
                    "fresh_reads": fresh_reads, "stable_reads": 0,
                }
            identity = (
                category,
                str(observed.get("fingerprint") or category),
                str(observed.get("document_fingerprint") or ""),
                str(observed.get("url") or ""),
            )
            if identity == last_identity:
                stable_reads += 1
            else:
                stable_reads = 1
                last_identity = identity
            if stable_reads >= 3:
                return {
                    "outcome": UNKNOWN_BLOCKER, "state": "unknown_dialog",
                    "present": True, "dismissed": handled,
                    "fresh_reads": fresh_reads, "stable_reads": stable_reads,
                    "fingerprint": identity[1],
                }

        if time.time() >= deadline and not post_click_read_pending:
            return {
                "outcome": TRANSITIONING_RETRY, "state": "",
                "present": present, "dismissed": handled,
                "fresh_reads": fresh_reads, "stable_reads": stable_reads,
            }
        time.sleep(0.2)
        observed = _observe(page)
        post_click_read_pending = False


def resolve_dialog_gate(page, *, allow_safe_close: bool = False, wait_seconds: float = 4.0) -> Dict[str, Any]:
    """Backward-compatible name for the shared continuation state machine."""
    return continue_after_dialog(
        page,
        allow_safe_close=allow_safe_close,
        wait_seconds=wait_seconds,
    )
