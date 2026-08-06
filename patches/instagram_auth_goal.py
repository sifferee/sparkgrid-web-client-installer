"""Shared goal-driven Instagram authentication continuation.

The goal is an authenticated browser session, not the completion of a fixed
sequence of optional Instagram dialogs.  Every browser workflow uses the
helpers in this module so a missing or reordered optional prompt cannot turn a
confirmed session into ``manual_required``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

from instagram_dialog_gate import (
    HANDLED_REEVALUATE,
    TRANSITIONING_RETRY,
    continue_after_dialog,
    inspect_dialog,
)
from instagram_consent_flow import consent_present, resolve_instagram_consent


AUTHENTICATED_CONFIRMED = "AUTHENTICATED_CONFIRMED"
ACTION_REQUIRED = "ACTION_REQUIRED"
TRANSITIONING = "TRANSITIONING_RETRY"
MANUAL_REQUIRED = "MANUAL_REQUIRED"


def _page_signals(page) -> Dict[str, Any]:
    """Return a fresh, secret-free URL/DOM/authentication observation."""
    defaults: Dict[str, Any] = {
        "ready_state": "",
        "loading": False,
        "dom_fingerprint": "",
        "login_form": False,
        "challenge_container": False,
        "auth_nav": False,
        "account_menu": False,
        "app_shell": False,
    }
    try:
        value = page.evaluate(
            """() => { // IG_AUTH_GOAL_OBSERVE
              const visible = (el) => {
                if (!el) return false;
                const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                return r.width>8 && r.height>8 && s.display!=='none' &&
                  s.visibility!=='hidden' && Number.parseFloat(s.opacity||'1')>0.01 &&
                  r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
              };
              const anyVisible = (selector) =>
                [...document.querySelectorAll(selector)].some(visible);
              const text=String((document.body && document.body.innerText)||'')
                .replace(/\\s+/g,' ').trim();
              const loginForm=anyVisible(
                "input[type='password'],input[name='password']," +
                "input[autocomplete='current-password']"
              );
              const challengeContainer=anyVisible(
                "form[action*='/challenge' i],form[action*='/checkpoint' i]," +
                "[data-testid*='challenge' i],[data-testid*='checkpoint' i]"
              );
              const authNav=anyVisible(
                "svg[aria-label='Home' i],svg[aria-label='New post' i]," +
                "a[href*='/direct/inbox'],nav a[href*='/direct/']"
              );
              const accountMenu=anyVisible(
                "nav img[alt*='profile picture' i]," +
                "a[href*='/accounts/edit']," +
                "[aria-label='Profile' i],[aria-label*='profile picture' i]"
              );
              const appShell=!!document.querySelector('main') &&
                anyVisible("nav,[role='navigation'],a[href='/']");
              const busy=anyVisible(
                "[aria-busy='true'],[role='progressbar']," +
                "svg[aria-label*='loading' i]"
              );
              const signature=[
                location.href,document.readyState,text.length,
                document.body ? document.body.childElementCount : 0,
                document.querySelectorAll("[role='dialog'],[aria-modal='true']").length,
                loginForm?'login':'',challengeContainer?'challenge':'',authNav?'nav':'',
                accountMenu?'account':'',appShell?'shell':''
              ].join('|');
              let hash=2166136261;
              for(let i=0;i<signature.length;i++){
                hash^=signature.charCodeAt(i);hash=Math.imul(hash,16777619);
              }
              return {
                ready_state:String(document.readyState||''),
                loading:document.readyState!=='complete'||busy||text.length===0,
                dom_fingerprint:(hash>>>0).toString(16),
                login_form:loginForm,challenge_container:challengeContainer,auth_nav:authNav,
                account_menu:accountMenu,app_shell:appShell
              };
            }"""
        )
        if isinstance(value, dict):
            for key in defaults:
                defaults[key] = value.get(key, defaults[key])
    except Exception:
        pass
    try:
        defaults["url"] = str(getattr(page, "url", "") or "")
    except Exception:
        defaults["url"] = ""
    try:
        cookies = page.context.cookies("https://www.instagram.com/")
        names = {
            str(item.get("name") or "").lower()
            for item in cookies
            if str(item.get("value") or "").strip()
        }
    except Exception:
        names = set()
    defaults["session_cookie"] = "sessionid" in names
    defaults["session_cookie_set"] = {
        "sessionid", "csrftoken", "ds_user_id"
    }.issubset(names)
    return defaults


def _current_user_endpoint(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """async () => { // IG_AUTH_GOAL_ENDPOINT
                  try {
                    const response=await fetch('/api/v1/accounts/current_user/',{
                      credentials:'include',
                      headers:{
                        'X-IG-App-ID':'936619743392459',
                        'X-Requested-With':'XMLHttpRequest'
                      }
                    });
                    if(response.status!==200) return false;
                    const data=await response.json();
                    const user=data&&(data.user||data);
                    return !!(user&&(user.pk||user.id)&&user.username);
                  } catch (_) { return false; }
                }"""
            )
        )
    except Exception:
        return False


def confirm_authenticated_state(
    page,
    observation: Dict[str, Any] | None = None,
    *,
    probe_endpoint: bool = True,
) -> Dict[str, Any]:
    """Confirm auth from one strong signal or a corroborating signal set.

    Cookie presence or a single navigation icon is never sufficient.  A
    visible login form always vetoes authentication.
    """
    observed = dict(observation or _page_signals(page))
    endpoint = _current_user_endpoint(page) if probe_endpoint else False
    login_form = bool(observed.get("login_form"))
    evidence = {
        "current_user_endpoint": endpoint,
        "authenticated_navigation": bool(observed.get("auth_nav")),
        "account_menu": bool(observed.get("account_menu")),
        "logged_in_shell": bool(observed.get("app_shell")),
        "session_cookie": bool(observed.get("session_cookie")),
        "session_cookie_set": bool(observed.get("session_cookie_set")),
        "no_active_login_form": not login_form,
    }
    ui_count = sum(
        int(evidence[key])
        for key in ("authenticated_navigation", "account_menu", "logged_in_shell")
    )
    corroborated = bool(
        evidence["no_active_login_form"]
        and (
            ui_count >= 3
            or (
                ui_count >= 2
                and (evidence["session_cookie"] or evidence["session_cookie_set"])
            )
        )
    )
    confirmed = bool(not login_form and (endpoint or corroborated))
    reason = (
        "authenticated current-user endpoint confirmed the session"
        if endpoint
        else (
            "corroborating authenticated UI/session signals confirmed the session"
            if corroborated
            else "authentication evidence is not yet sufficient"
        )
    )
    return {
        "confirmed": confirmed,
        "reason": reason,
        "evidence": evidence,
        "observation": observed,
    }


@dataclass
class AuthenticationGoalStateMachine:
    """Order-independent observation epochs, action ledger and watchdog."""

    required_unknown_reads: int = 3
    epoch: int = 0
    action_ledger: set[tuple[int, str, str]] = field(default_factory=set)
    last_identity: tuple[str, str, str] | None = None
    stable_unknown_reads: int = 0
    progress_count: int = 0
    auth_reached: bool = False

    def observe(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        url = str(observation.get("url") or "")
        dom = str(observation.get("dom_fingerprint") or "")
        state = str(observation.get("state") or "")
        identity = (url, dom, state)
        changed = self.last_identity is not None and identity != self.last_identity
        if changed:
            self.epoch += 1
            self.action_ledger.clear()
            self.stable_unknown_reads = 0
            self.progress_count += 1
        self.last_identity = identity
        return {"changed": changed, "epoch": self.epoch}

    def action_allowed(self, action: str, fingerprint: str) -> bool:
        return (self.epoch, str(action), str(fingerprint)) not in self.action_ledger

    def record_action(self, action: str, fingerprint: str) -> None:
        self.action_ledger.add((self.epoch, str(action), str(fingerprint)))
        # Any action invalidates the observation that authorized it.
        self.epoch += 1
        self.last_identity = None
        self.stable_unknown_reads = 0
        self.progress_count += 1

    def stable_unknown(self, fingerprint: str, *, loading: bool, changed: bool) -> bool:
        if loading or changed or not fingerprint:
            self.stable_unknown_reads = 0
            return False
        self.stable_unknown_reads += 1
        return self.stable_unknown_reads >= self.required_unknown_reads


def continue_authentication_goal(
    page,
    *,
    timeout_seconds: float = 8.0,
    authenticated_hint: bool = False,
    optional_cleanup: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Observe until auth, an actionable state, or a stable real blocker.

    Authentication is checked first on every fresh read.  Once reached it is
    retained even if bounded cleanup of an optional dialog cannot complete.
    """
    deadline = time.time() + max(0.0, float(timeout_seconds))
    machine = AuthenticationGoalStateMachine()
    machine.auth_reached = bool(authenticated_hint)
    fresh_reads = 0
    while True:
        fresh_reads += 1
        observation = _page_signals(page)
        dialog = inspect_dialog(page)
        observation.update(
            {
                "dialog_category": str(dialog.get("category") or ""),
                "dialog_fingerprint": str(dialog.get("fingerprint") or ""),
                "dialog_present": bool(dialog.get("present")),
                "consent_present": bool(consent_present(page)),
            }
        )
        progress = machine.observe(observation)
        auth = confirm_authenticated_state(page, observation)
        if auth["confirmed"]:
            machine.auth_reached = True

        category = observation["dialog_category"]
        url = str(observation.get("url") or "").lower()
        if category in {"checkpoint", "restriction", "suspended"}:
            return {
                "ok": False,
                "state": {
                    "restriction": "restricted",
                    "checkpoint": "checkpoint",
                    "suspended": "suspended",
                }[category],
                "goal": MANUAL_REQUIRED,
                "manual_required": True,
                "authenticated": False,
                "fresh_reads": fresh_reads,
            }
        if (
            "/challenge/" in url
            or "/checkpoint/" in url
            or observation.get("challenge_container")
        ):
            return {
                "ok": False, "state": "checkpoint",
                "goal": MANUAL_REQUIRED, "manual_required": True,
                "authenticated": False, "fresh_reads": fresh_reads,
            }

        if machine.auth_reached:
            cleanup = None
            if optional_cleanup and observation["consent_present"]:
                cleanup = resolve_instagram_consent(
                    page, max_seconds=min(2.0, timeout_seconds)
                )
            elif optional_cleanup and observation["dialog_present"]:
                cleanup = continue_after_dialog(
                    page, allow_safe_close=True, wait_seconds=min(2.0, timeout_seconds)
                )
            return {
                "ok": True,
                "state": "logged_in",
                "goal": AUTHENTICATED_CONFIRMED,
                "reason": auth["reason"] if auth["confirmed"] else "authentication was already confirmed",
                "authenticated": True,
                "auth_reached": True,
                "manual_required": False,
                "operationally_ready": True,
                "optional_cleanup": cleanup,
                "fresh_reads": fresh_reads,
                "epoch": machine.epoch,
                "evidence": auth["evidence"],
            }

        if observation["consent_present"]:
            consent = resolve_instagram_consent(
                page, max_seconds=min(4.0, timeout_seconds)
            )
            if consent.get("handled") or consent.get("ok"):
                machine.record_action("consent", "consent")
                if time.time() < deadline:
                    continue
            elif consent.get("manual_required"):
                return {
                    "ok": False, "state": "consent_required",
                    "goal": MANUAL_REQUIRED, "manual_required": True,
                    "authenticated": False, "fresh_reads": fresh_reads,
                }
        if observation["dialog_present"]:
            if category in {
                "cookie_consent", "save_login", "notification", "policy_notice"
            }:
                continuation = continue_after_dialog(
                    page, allow_safe_close=True, wait_seconds=min(4.0, timeout_seconds)
                )
                if continuation.get("outcome") in {
                    HANDLED_REEVALUATE, TRANSITIONING_RETRY
                } or continuation.get("dismissed"):
                    machine.record_action(
                        str(continuation.get("clicked_action") or category),
                        observation["dialog_fingerprint"],
                    )
                    if time.time() < deadline:
                        continue
        if "two_step_verification" in url or "two_factor" in url:
            return {
                "ok": False, "state": "two_factor_required",
                "goal": ACTION_REQUIRED, "manual_required": False,
                "authenticated": False, "fresh_reads": fresh_reads,
            }
        if observation.get("login_form"):
            return {
                "ok": False, "state": "login_required",
                "goal": ACTION_REQUIRED, "manual_required": False,
                "authenticated": False, "fresh_reads": fresh_reads,
            }
        if observation.get("loading") or progress["changed"]:
            machine.stable_unknown_reads = 0
        else:
            fingerprint = (
                observation["dialog_fingerprint"]
                if observation["dialog_present"]
                else str(observation.get("dom_fingerprint") or "")
            )
            if machine.stable_unknown(
                fingerprint, loading=False, changed=bool(progress["changed"])
            ):
                return {
                    "ok": False,
                    "state": "unknown_popup" if observation["dialog_present"] else "unknown",
                    "goal": MANUAL_REQUIRED,
                    "manual_required": True,
                    "authenticated": False,
                    "fresh_reads": fresh_reads,
                    "stable_reads": machine.stable_unknown_reads,
                    "fingerprint": fingerprint,
                }
        if time.time() >= deadline:
            return {
                "ok": False, "state": "transitioning",
                "goal": TRANSITIONING, "manual_required": False,
                "authenticated": False, "fresh_reads": fresh_reads,
            }
        sleep(0.25)
