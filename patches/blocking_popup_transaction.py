"""Structural, mutation-aware transaction for Instagram blocking containers.

No raw text, URL, DOM, selector, class name, or control label crosses this
module's public boundary.  Every action re-inspects the current topmost
container and clicks one freshly enumerated control inside that container.
"""
from __future__ import annotations

import time
from typing import Any, Callable
from log_config import get_logger

logger = get_logger("automation")


BLOCKER_CATEGORIES = {
    "cookie_consent",
    "regional_ads_consent",
    "request_processing",
    "save_login_info",
    "notifications_prompt",
    "promo_or_ad",
    "open_in_app",
    "unknown_blocker",
}
AUTOMATED_POPUP_CATEGORIES = BLOCKER_CATEGORIES - {"unknown_blocker"}
ADS_ERRORS = {
    "ads_consent_action_unavailable",
    "ads_consent_loop_detected",
    "ads_consent_transition_timeout",
}
MAX_ADS_TRANSITIONS = 8
MAX_ADS_SUCCESSOR_READS = 40
ADS_SUCCESSOR_SETTLED_READS = 3
MAX_CONSENT_CHAIN_STEPS = 8
MAX_CONSENT_ACTION_RETRIES = 3
COOKIE_ACTIONS = {"cookie_allow_all", "cookie_decline_optional"}
REQUEST_PROCESSING_ACTIONS = {"request_processing_ok"}
REGIONAL_ADS_ACTIONS = {
    "ads_get_started",
    "ads_select_free",
    "ads_continue",
    "ads_agree",
    "ads_personalized_continue",
    "ads_confirm",
    "ads_ok",
}

# Only these two reasons mean "structural/text search ran clean and
# genuinely found nothing" — NOT that something crashed. interaction_failed
# (and anything else) means an exception happened inside perform_fresh_action
# and was already logged via except-block logging; vision must never be used
# to paper over that, or we'd recreate the exact failure mode that cost this
# project two weeks (a real code bug silently "working around itself").
_VISION_ELIGIBLE_REASONS = frozenset({"action_unavailable", "container_missing"})

_ACTION_VISION_INTENT = {
    "cookie_allow_all": "the button that allows/accepts all cookies",
    "cookie_decline_optional": "the button that declines optional cookies",
    "request_processing_ok": "the OK button on a request-processing error message",
    "ads_get_started": "the 'Get started' button for the ads consent flow",
    "ads_select_free": "the option to use the free, ad-supported version",
    "ads_continue": "the 'Continue' button",
    "ads_agree": "the 'Agree' or 'I agree' button consenting to data processing",
    "ads_personalized_continue": (
        "the button confirming personalized ads / "
        "'Continue with personalized ads'"
    ),
    "ads_confirm": "the 'Confirm' button",
    "ads_ok": "an 'OK' confirmation button",
}


def _attempt_vision_fallback(
    page: Any,
    action: str,
    reason: str,
    *,
    event_fn: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Last resort: ask a vision model to find and click the element, but
    ONLY when structural/text matching completed cleanly and found nothing
    (reason in _VISION_ELIGIBLE_REASONS). Never call this for a reason
    that came from an exception — that needs a real code fix, not a
    vision workaround; see vision_fallback.py's module docstring.

    Fails gracefully (returns {"ok": False, ...}) if the vision module or
    its API key isn't available — this must never be why the automation
    itself breaks.
    """
    if reason not in _VISION_ELIGIBLE_REASONS:
        return {"ok": False, "reason": "vision_not_eligible", "detail": reason}
    try:
        import vision_fallback
    except ImportError as _exc:
        logger.debug("vision_fallback unavailable: %s", _exc)
        return {"ok": False, "reason": "vision_unavailable", "detail": "module_not_available"}

    intent = _ACTION_VISION_INTENT.get(action, f"the button for the action '{action}'")
    logger.info("vision_fallback: attempting action=%s intent=%r", action, intent)
    _emit_consent_event(event_fn, "vision_fallback_attempted", action=action)
    result = vision_fallback.click_via_vision(page, intent=intent)
    logger.info(
        "vision_fallback: result action=%s ok=%s reason=%s",
        action, result.get("ok"), result.get("reason"),
    )
    _emit_consent_event(
        event_fn,
        "vision_fallback_result",
        action=action,
        ok=str(bool(result.get("ok"))),
        reason=str(result.get("reason") or ""),
    )
    return result


_INSPECT_SCRIPT = r"""() => { // IG_BLOCKING_POPUP_INSPECT
  if (!/^https?:$/.test(location.protocol)) {
    return {present:false, document_category:'browser_internal_error'};
  }
  const stateKey=Symbol.for('sparkgrid.blocker.transaction.v1');
  let state=globalThis[stateKey];
  if (!state || state.documentElement!==document.documentElement) {
    state={
      documentElement:document.documentElement,
      documentEpoch:(Date.now().toString(36)+Math.random().toString(36).slice(2)),
      mutationEpoch:0
    };
    try {
      new MutationObserver(()=>{state.mutationEpoch+=1;}).observe(
        document.documentElement,{subtree:true,childList:true,attributes:true}
      );
    } catch (_) {}
    globalThis[stateKey]=state;
  }
  const norm=v=>String(v||'').replace(/\s+/g,' ').trim().toLowerCase();
  const visible=el=>{
    if(!el || !el.isConnected)return false;
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    if(!(r.width>8&&r.height>8&&r.bottom>0&&r.right>0&&r.top<innerHeight&&
      r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&
      Number.parseFloat(s.opacity||'1')>.01))return false;
    for(let n=el;n&&n.nodeType===1;n=n.parentElement){
      const x=getComputedStyle(n);
      if(n.hidden||n.inert||n.getAttribute('aria-hidden')==='true'||
        x.display==='none'||x.visibility==='hidden'||
        Number.parseFloat(x.opacity||'1')<=.01)return false;
    }
    return true;
  };
  const controlsOf=container=>[...container.querySelectorAll(
    "button,input[type='button'],input[type='submit'],[role='button'],[role='radio'],label,div[tabindex],a[href]"
  )].filter(visible);
  const editable=el=>visible(el)&&!el.disabled&&!el.readOnly&&
    el.getAttribute('aria-disabled')!=='true'&&
    !el.hasAttribute('readonly');
  const username=[...document.querySelectorAll(
    "input[name='username'],input[autocomplete='username'],input[name='email'],"+
    "form input[type='text'],form input[type='email'],form input[type='tel']"
  )].find(editable);
  const password=[...document.querySelectorAll(
    "input[type='password'],input[autocomplete='current-password']"
  )].find(editable);
  const login=!!username&&!!password;
  const auth=!!document.querySelector(
    "a[href*='/direct/inbox'],a[href*='/accounts/edit'],svg[aria-label='Home']"
  );
  const otp=!![...document.querySelectorAll(
    "input[autocomplete='one-time-code'],input[inputmode='numeric']"
  )].find(editable);
  let candidates=[...document.querySelectorAll(
    "[role='dialog'],[aria-modal='true'],[data-visualcompletion='ignore-dynamic']"
  )].filter(visible).map((el,index)=>{
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    const area=Math.min(innerWidth,Math.max(0,r.right)-Math.max(0,r.left))*
      Math.min(innerHeight,Math.max(0,r.bottom)-Math.max(0,r.top));
    const modal=el.getAttribute('aria-modal')==='true'||el.getAttribute('role')==='dialog';
    const controls=controlsOf(el);
    const z=Number.parseInt(s.zIndex,10);
    return {el,index,area,modal,controls,
      score:(Number.isFinite(z)?z:0)*1e9+(modal?5e8:0)+area+index};
  }).filter(x=>x.controls.length>0).sort((a,b)=>b.score-a.score);
  // Cookie, request-processing, and ads successors can be full-page roots.
  // Admit one only from a complete exact signature; a URL or generic button
  // is insufficient evidence.
  if(!candidates.length){
    const root=document.querySelector('main')||document.body;
    const controls=controlsOf(root);
    const labels=controls.map(el=>norm(el.getAttribute('aria-label')||
      el.getAttribute('value')||el.innerText||el.textContent));
    const rootText=norm(root.innerText||root.textContent);
    const typedCookie=
      rootText.includes('allow the use of cookies by instagram')&&
      labels.includes('allow all cookies')&&
      labels.includes('decline optional cookies');
    const typedRequest=
      (rootText.includes("your request couldn't be processed")||
       rootText.includes("your request can't be processed")||
       rootText.includes('your request couldn\u2019t be processed')||
       rootText.includes('your request can\u2019t be processed'))&&
      rootText.includes('there was a problem with this request')&&
      rootText.includes('try again later')&&labels.includes('ok');
    const typedPersonalizedAds=
      (location.pathname||'').toLowerCase().includes('/consent')&&
      labels.includes('continue with personalized ads')&&
      labels.includes('switch to less-personalized ads');
    const typedAdsConfirmation=
      (location.pathname||'').toLowerCase().includes('/consent')&&
      labels.includes('confirm')&&labels.includes('go back');
    if(typedCookie||typedRequest||typedPersonalizedAds||typedAdsConfirmation){
      const r=root.getBoundingClientRect();
      const area=Math.min(innerWidth,Math.max(0,r.right)-Math.max(0,r.left))*
        Math.min(innerHeight,Math.max(0,r.bottom)-Math.max(0,r.top));
      candidates=[{el:root,index:0,area,modal:false,controls,score:area}];
    }
  }
  const top=candidates[0];
  if(!top)return {
    present:false,category:'',fingerprint:'',document_epoch:state.documentEpoch,
    mutation_epoch:state.mutationEpoch,document_category:'instagram_document',
    authenticated_surface:auth,login_surface:login,two_factor_surface:otp
  };
  const text=norm(top.el.innerText||top.el.textContent);
  const controls=top.controls.map((el,index)=>({
    index,
    label:norm(el.getAttribute('aria-label')||el.getAttribute('value')||
      el.innerText||el.textContent),
    role:norm(el.getAttribute('role')||el.tagName),
    enabled:!el.disabled&&el.getAttribute('aria-disabled')!=='true',
    selected:el.checked===true||el.getAttribute('aria-checked')==='true'
  }));
  const labels=controls.map(x=>x.label);
  const has=(...items)=>items.some(item=>text.includes(item)||
    labels.some(label=>label.includes(item)));
  const exact=(...items)=>items.some(item=>labels.includes(item));
  const anyLabel=pattern=>labels.some(label=>pattern.test(label));
  const localized={
    notNow:/^(not now|jetzt nicht|ahora no|pas maintenant|non ora|agora não)$/,
    continue:/^(continue|weiter|continuar|continuer|continua)$/,
    agree:/^(agree|zustimmen|aceptar|accepter|accetta|concordar)$/,
    getStarted:/^(get started|los geht.?s|empezar|commencer|inizia|começar)$/,
    allowAll:/^(allow all cookies|alle cookies erlauben|permitir todas las cookies|autoriser tous les cookies|consenti tutti i cookie|permitir todos os cookies)$/,
    declineOptional:/^(decline optional cookies|optionale cookies ablehnen|rechazar cookies opcionales|refuser les cookies facultatifs|rifiuta cookie facoltativi|recusar cookies opcionais)$/,
    freeAds:/(free.*ads|kostenlos.*werbung|gratis.*anunci|gratuit.*publicit|gratuitamente.*pubblic|grátis.*anúnci)/,
    personalized:/(personalized ads|personalisierte werbung|anuncios personalizados|publicités personnalisées|inserzioni personalizzate|anúncios personalizados)/
  };
  const hasMedia=!!top.el.querySelector(
    "video,canvas,input[type='file'],textarea,[contenteditable='true']"
  );
  const progress=!!top.el.querySelector(
    "[role='progressbar'],[aria-busy='true']"
  );
  const cookieManagerV2=
    has('allow the use of cookies by instagram')&&
    anyLabel(localized.allowAll)&&anyLabel(localized.declineOptional);
  let category='unknown_blocker',adsStep='',action='',cookieVariant='';
  if(hasMedia&&has('next','share','post','publish','crop','cover photo')){
    category='operation_composer';
  }else if(progress&&has('sharing','posting','publishing','processing','uploading')){
    category='operation_processing';
  }else if(has('shared successfully','reel shared')){
    category='operation_success';
  }else if(has('suspended','disabled')){
    category='suspended';
  }else if(has('challenge','checkpoint','confirm it is you','confirm it’s you','verification')){
    category='checkpoint';
  }else if(has('try again later','restricted','suspicious activity')){
    category='restriction';
  }else if(
    has("your request couldn't be processed","your request can't be processed",
      'your request couldn\u2019t be processed','your request can\u2019t be processed')&&
    has('there was a problem with this request')&&has('try again later')&&
    exact('ok')
  ){
    category='request_processing';action='request_processing_ok';
  }else if(cookieManagerV2){
    category='cookie_consent';cookieVariant='cookie_manager_v2';
    action='cookie_allow_all';
  }else if(
    (location.pathname||'').toLowerCase().includes('/consent')&&
    (has('ads','ad experience','free of charge','subscribe','data for ads',
      'personalized ads','meta processing')||anyLabel(localized.freeAds)||
      anyLabel(localized.getStarted)||anyLabel(localized.agree)||
      anyLabel(localized.continue)||anyLabel(localized.personalized))
  ){
    category='regional_ads_consent';
    if(anyLabel(localized.freeAds)){
      const selected=controls.some(x=>x.selected&&
        localized.freeAds.test(x.label));
      adsStep=selected?'free_with_ads_selected':'free_with_ads_choice';
      action=selected?'ads_continue':'ads_select_free';
    }else if(anyLabel(localized.getStarted)){
      adsStep='ads_intro';action='ads_get_started';
    }else if(anyLabel(localized.agree)){
      adsStep='processing_agreement';action='ads_agree';
    }else if(anyLabel(localized.personalized)){
      adsStep='ad_experience';action='ads_personalized_continue';
    }else if(exact('confirm')&&exact('go back')){
      adsStep='personalized_ads_confirmation';action='ads_confirm';
    }else if(exact('ok')){
      adsStep='confirmation';action='ads_ok';
    }else if(anyLabel(localized.continue)){
      adsStep='continuation';action='ads_continue';
    }else{
      adsStep='unknown_ads_step';
    }
  }else if((has('cookie','cookies')||anyLabel(localized.allowAll)||
    anyLabel(localized.declineOptional))&&
    (has('allow all','optional cookies','essential cookies')||labels.length>=2)){
    category='cookie_consent';
    cookieVariant='cookie_dialog_legacy';
    action=anyLabel(localized.allowAll)?'cookie_allow_all':
      anyLabel(localized.declineOptional)?'cookie_decline_optional':'';
  }else if(has('save your login info','save login information',
    'anmeldeinformationen speichern','guardar información de inicio',
    'enregistrer vos informations de connexion','salva le informazioni di accesso')&&
    has('not now','later','save info')){
    category='save_login_info';action=anyLabel(localized.notNow)?'dismiss_not_now':'';
  }else if(has('notifications','turn on notifications','benachrichtigungen',
    'notificaciones','notifications','notifiche')&&
    (has('not now','later')||anyLabel(localized.notNow))){
    category='notifications_prompt';action=anyLabel(localized.notNow)?'dismiss_not_now':'';
  }else if(has('open in app','continue in app','use the app')){
    category='open_in_app';action=anyLabel(localized.notNow)?'dismiss_not_now':
      exact('cancel')?'dismiss_cancel':'';
  }else if(has('sponsored','promotion','special offer','ad preferences')&&
    has('close','not now','dismiss')){
    category='promo_or_ad';action=anyLabel(localized.notNow)?'dismiss_not_now':
      exact('close')?'dismiss_close':'';
  }
  const knownPopup=[
    'cookie_consent','regional_ads_consent','request_processing','save_login_info',
    'notifications_prompt','promo_or_ad','open_in_app'
  ].includes(category);
  const knownSurface=knownPopup||[
    'operation_composer','operation_processing','operation_success',
    'checkpoint','restriction','suspended'
  ].includes(category);
  const signature=[
    category,adsStep,cookieVariant,top.modal?'modal':'overlay',
    Math.round(top.area/1000),controls.map(x=>x.role+':'+x.label+':'+x.enabled+':'+x.selected).join('|')
  ].join('|');
  let hash=2166136261;
  for(let i=0;i<signature.length;i++){hash^=signature.charCodeAt(i);
    hash=Math.imul(hash,16777619);}
  return {
    present:knownSurface,category:knownSurface?category:'',
    unrecognized_surface:!knownSurface&&!login&&!auth&&!otp,
    ads_step:adsStep,cookie_variant:cookieVariant,
    recommended_action:knownPopup?action:'',
    fingerprint:(hash>>>0).toString(16),
    document_epoch:state.documentEpoch,mutation_epoch:state.mutationEpoch,
    control_count:controls.length,authenticated_surface:auth,
    login_surface:login,two_factor_surface:otp,
    document_category:'instagram_document'
  };
}"""


_ACTION_SCRIPT = r"""payload => { // IG_BLOCKING_POPUP_ACTION
  const norm=v=>String(v||'').replace(/\s+/g,' ').trim().toLowerCase();
  const visible=el=>{
    if(!el||!el.isConnected)return false;
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>8&&r.height>8&&r.bottom>0&&r.right>0&&r.top<innerHeight&&
      r.left<innerWidth&&s.display!=='none'&&s.visibility!=='hidden'&&
      Number.parseFloat(s.opacity||'1')>.01&&!el.disabled&&
      el.getAttribute('aria-disabled')!=='true';
  };
  const rx={
    allowAll:/^(allow all cookies|alle cookies erlauben|permitir todas las cookies|autoriser tous les cookies|consenti tutti i cookie|permitir todos os cookies)$/,
    declineOptional:/^(decline optional cookies|optionale cookies ablehnen|rechazar cookies opcionales|refuser les cookies facultatifs|rifiuta cookie facoltativi|recusar cookies opcionais)$/,
    notNow:/^(not now|jetzt nicht|ahora no|pas maintenant|non ora|agora não)$/,
    getStarted:/^(get started|los geht.?s|empezar|commencer|inizia|começar)$/,
    freeAds:/(free.*ads|kostenlos.*werbung|gratis.*anunci|gratuit.*publicit|gratuitamente.*pubblic|grátis.*anúnci)/,
    continue:/^(continue|weiter|continuar|continuer|continua)$/,
    agree:/^(agree|zustimmen|aceptar|accepter|accetta|concordar)$/,
    personalized:/(personalized ads|personalisierte werbung|anuncios personalizados|publicités personnalisées|inserzioni personalizzate|anúncios personalizados)/
  };
  let candidates=[...document.querySelectorAll(
    "[role='dialog'],[aria-modal='true'],[data-visualcompletion='ignore-dynamic']"
  )].filter(visible).map((el,index)=>{
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    const area=Math.min(innerWidth,Math.max(0,r.right)-Math.max(0,r.left))*
      Math.min(innerHeight,Math.max(0,r.bottom)-Math.max(0,r.top));
    const modal=el.getAttribute('aria-modal')==='true'||el.getAttribute('role')==='dialog';
    const controls=[...el.querySelectorAll(
      "button,input[type='button'],input[type='submit'],[role='button'],[role='radio'],label,div[tabindex],a[href]"
    )].filter(visible);
    const z=Number.parseInt(s.zIndex,10);
    return {el,controls,score:(Number.isFinite(z)?z:0)*1e9+
      (modal?5e8:0)+area+index};
  }).filter(x=>x.controls.length>0).sort((a,b)=>b.score-a.score);
  if(!candidates.length){
    const root=document.querySelector('main')||document.body;
    const controls=[...root.querySelectorAll(
      "button,input[type='button'],input[type='submit'],[role='button'],[role='radio'],label,div[tabindex],a[href]"
    )].filter(visible);
    const labels=controls.map(el=>norm(el.getAttribute('aria-label')||
      el.getAttribute('value')||el.innerText||el.textContent));
    const rootText=norm(root.innerText||root.textContent);
    const typedCookie=(rootText.includes('allow the use of cookies by instagram')||
        rootText.includes('allow the use of cookies')||rootText.includes('cookie'))&&
        (labels.includes('allow all cookies')||labels.includes('decline optional cookies')||
         labels.some(l=>/^allow/i.test(l))||labels.some(l=>/^accept/i.test(l)));
      const typedRequest=
        (rootText.includes("your request couldn't be processed")||
         rootText.includes("your request can't be processed")||
         rootText.includes('your request couldn\u2019t be processed')||
         rootText.includes('your request can\u2019t be processed'))&&
        rootText.includes('there was a problem with this request')&&
        rootText.includes('try again later')&&labels.includes('ok');
      const consentPath=(location.pathname||'').toLowerCase().includes('/consent');
      // Broader consent detection: any page on /consent/ with Agree/Allow/Accept/Continue button
      const typedConsent=consentPath&&(labels.some(l=>rx.agree.test(l))||
        labels.some(l=>/^allow/i.test(l))||labels.some(l=>/^accept$/i.test(l))||
        labels.some(l=>rx.continue.test(l))||labels.some(l=>rx.freeAds.test(l))||
        labels.some(l=>rx.getStarted.test(l))||labels.some(l=>rx.personalized.test(l)));
      const typedPersonalized=consentPath&&
        labels.includes('continue with personalized ads')&&
        labels.includes('switch to less-personalized ads');
      const typedConfirmation=consentPath&&labels.includes('confirm')&&
        labels.includes('go back');
      if(typedCookie||typedRequest||typedPersonalized||typedConfirmation||typedConsent){
      candidates=[{el:root,controls,score:0}];
    }
  }
  const top=candidates[0];if(!top)return {ok:false,reason:'container_missing'};
  const label=el=>norm(el.getAttribute('aria-label')||el.getAttribute('value')||
    el.innerText||el.textContent);
  const match={
    cookie_allow_all:x=>rx.allowAll.test(x),
    cookie_decline_optional:x=>rx.declineOptional.test(x),
    dismiss_not_now:x=>rx.notNow.test(x),
    dismiss_cancel:x=>x==='cancel',
    dismiss_close:x=>x==='close',
    ads_get_started:x=>rx.getStarted.test(x),
    ads_select_free:x=>rx.freeAds.test(x),
    ads_continue:x=>rx.continue.test(x),
    ads_agree:x=>rx.agree.test(x),
    // The successor exposes both "Continue with personalized ads" and
    // "Switch to less-personalized ads". Match the intended typed choice
    // exactly so the action cannot become ambiguous.
    ads_personalized_continue:x=>x==='continue with personalized ads',
    ads_confirm:x=>x==='confirm',
    ads_ok:x=>x==='ok',
    request_processing_ok:x=>x==='ok'
  }[payload.action];
  if(!match)return {ok:false,reason:'action_not_allowed'};
  const matches=top.controls.filter(el=>match(label(el)));
  if(matches.length===1)return {ok:true,reason:'ready',target:matches[0]};
  if(matches.length>1)return {ok:false,reason:'action_ambiguous'};
  // ─── TEXT-BASED FALLBACK ───
  // CSS selectors missed the button (Instagram may render it as <div>, <span>,
  // or any custom element).  Scan ALL visible elements for text match.
  const actionLabels={
    cookie_allow_all:['allow all cookies','alle cookies erlauben','permitir todas las cookies','autoriser tous les cookies','consenti tutti i cookie','permitir todos os cookies'],
    cookie_decline_optional:['decline optional cookies','optionale cookies ablehnen','rechazar cookies opcionales','refuser les cookies facultatifs','rifiuta cookie facoltativi','recusar cookies opcionais'],
    dismiss_not_now:['not now','jetzt nicht','ahora no','pas maintenant','non ora','agora não'],
    dismiss_cancel:['cancel'],
    dismiss_close:['close'],
    ads_get_started:['get started','los geht','empezar','commencer','inizia','começar'],
    ads_select_free:['use my info for a free experience','free ads','kostenlose werbung','gratis anunci','gratuit publicit','gratuitamente pubblic'],
    ads_continue:['continue','weiter','continuar','continuer','continua'],
    ads_agree:['agree','zustimmen','aceptar','accepter','accetta','concordar'],
    ads_personalized_continue:['continue with personalized ads','personalisierte werbung','anuncios personalizados','publicités personnalisées','inserzioni personalizzate','anúncios personalizados'],
    ads_confirm:['confirm'],
    ads_ok:['ok'],
    request_processing_ok:['ok']
  }[payload.action]||[];
  if(actionLabels.length){
    const all=[...document.querySelectorAll('*')].filter(visible);
    for(const el of all){
      const t=norm(el.getAttribute('aria-label')||el.innerText||el.textContent);
      if(!t||t.length>60)continue;
      if(actionLabels.some(al=>t.toLowerCase()===al||t.toLowerCase().includes(al))){
        return {ok:true,reason:'text_fallback',target:el};
      }
    }
  }
  return {ok:false,reason:'action_unavailable'};
}"""


def _safe_observation(value: Any, frame_ref: str = "") -> dict[str, Any]:
    raw = dict(value or {}) if isinstance(value, dict) else {}
    category = str(raw.get("category") or "")
    if category and category not in BLOCKER_CATEGORIES | {
        "operation_composer",
        "operation_processing",
        "operation_success",
        "checkpoint",
        "restriction",
        "suspended",
    }:
        category = "unknown_blocker"
    return {
        "present": bool(raw.get("present")),
        "category": category,
        "ads_step": str(raw.get("ads_step") or "")[:80],
        "cookie_variant": str(raw.get("cookie_variant") or "")[:80],
        "recommended_action": str(raw.get("recommended_action") or "")[:80],
        "fingerprint": str(raw.get("fingerprint") or "")[:80],
        "document_epoch": str(raw.get("document_epoch") or "")[:80],
        "mutation_epoch": max(0, int(raw.get("mutation_epoch") or 0)),
        "control_count": max(0, min(int(raw.get("control_count") or 0), 100)),
        "authenticated_surface": bool(raw.get("authenticated_surface")),
        "login_surface": bool(raw.get("login_surface")),
        "two_factor_surface": bool(raw.get("two_factor_surface")),
        "unrecognized_surface": bool(raw.get("unrecognized_surface")),
        "document_category": str(raw.get("document_category") or "unknown_document"),
        "frame_ref": str(frame_ref or ""),
    }


def _frames(page: Any) -> list[Any]:
    try:
        values = list(page.frames)
        if values:
            return values
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass
    return [page]


def inspect_topmost_blocker(page: Any) -> dict[str, Any]:
    """Inspect each live frame and return one topmost structural blocker."""
    observations = []
    nonblocking = []
    for index, frame in enumerate(_frames(page)):
        try:
            value = frame.evaluate(_INSPECT_SCRIPT)
        except Exception as _exc:
            logger.debug("%s: %s", type(_exc).__name__, _exc)
            continue
        observed = _safe_observation(value, f"frame-{index + 1}")
        if observed["document_category"] == "browser_internal_error":
            return observed
        if observed["present"]:
            observations.append(observed)
        else:
            nonblocking.append(observed)
    if not observations:
        return nonblocking[0] if nonblocking else _safe_observation({})
    # A main-frame modal wins. If only a child frame owns a blocker, the last
    # visible frame is the topmost browser-composited owner available to us.
    return observations[0] if observations[0]["frame_ref"] == "frame-1" else observations[-1]


def perform_fresh_action(
    page: Any,
    observation: dict[str, Any],
    action: str,
    *,
    human: Any = None,
    event_fn: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Reacquire one typed target and dispatch it through HumanInteractor."""
    current = inspect_topmost_blocker(page)
    if (
        not current.get("present")
        or current.get("category") != observation.get("category")
        or current.get("fingerprint") != observation.get("fingerprint")
        or current.get("document_epoch") != observation.get("document_epoch")
    ):
        return {"ok": False, "reason": "stale_observation"}
    frame_ref = str(current.get("frame_ref") or "")
    try:
        index = max(0, int(frame_ref.split("-")[-1]) - 1)
        frame = _frames(page)[index]
        # Narrow compatibility for structural unit-test doubles. Real
        # Playwright frames always expose evaluate_handle and therefore always
        # use the HumanInteractor path below.
        if not hasattr(frame, "evaluate_handle"):
            raw = frame.evaluate(_ACTION_SCRIPT, {"action": str(action or "")})
            value = dict(raw or {}) if isinstance(raw, dict) else {}
            return {
                "ok": bool(value.get("ok")),
                "reason": str(value.get("reason") or "interaction_failed")[:80],
            }
        handle = frame.evaluate_handle(
            _ACTION_SCRIPT, {"action": str(action or "")}
        )
        reason_handle = handle.get_property("reason")
        reason = str(reason_handle.json_value() or "interaction_failed")[:80]
        ok_handle = handle.get_property("ok")
        ready = bool(ok_handle.json_value())
        target_handle = handle.get_property("target") if ready else None
        target = target_handle.as_element() if target_handle is not None else None
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        return {"ok": False, "reason": "interaction_failed"}
    if not ready or target is None:
        return {"ok": False, "reason": reason}
    if human is None or not hasattr(human, "click"):
        return {"ok": False, "reason": "human_interactor_unavailable"}
    category = str(current.get("category") or "unknown_blocker")[:80]
    _emit_consent_event(
        event_fn, "human_action_started", action=action, category=category
    )
    _emit_consent_event(
        event_fn, "human_target_classified", action=action, category=category
    )
    try:
        clicked = bool(human.click(target, timeout=5000))
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        clicked = False
    if not clicked:
        _emit_consent_event(
            event_fn,
            "human_action_completed",
            action=action,
            category=category,
            success=False,
        )
        return {"ok": False, "reason": "human_click_failed"}
    _emit_consent_event(
        event_fn, "human_click_dispatched", action=action, category=category
    )
    _emit_consent_event(
        event_fn,
        "human_action_completed",
        action=action,
        category=category,
        success=True,
    )
    return {"ok": True, "reason": "dispatched"}


def _changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(
        not after.get("present")
        or after.get("fingerprint") != before.get("fingerprint")
        or after.get("mutation_epoch") != before.get("mutation_epoch")
        or after.get("document_epoch") != before.get("document_epoch")
        or after.get("category") != before.get("category")
        or after.get("ads_step") != before.get("ads_step")
        or after.get("authenticated_surface")
        or after.get("login_surface")
        or after.get("two_factor_surface")
    )


def wait_for_transition(
    page: Any,
    before: dict[str, Any],
    *,
    timeout_seconds: float,
    inspect_fn: Callable[[Any], dict[str, Any]] = inspect_topmost_blocker,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        after = inspect_fn(page)
        if _changed(before, after):
            return after
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    after = inspect_fn(page)
    return after if _changed(before, after) else None


def _wait_for_ads_successor(
    page: Any,
    before: dict[str, Any],
    *,
    transition_timeout: float,
    successor_grace: float,
    settled_reads_required: int,
    max_successor_reads: int,
    overall_deadline: float,
    poll_interval: float,
    inspect_fn: Callable[[Any], dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, int]:
    """Wait for a fresh, authoritative successor to one regional-ads action."""
    transition_deadline = min(
        overall_deadline,
        time.monotonic() + max(0.0, float(transition_timeout)),
    )
    successor_deadline = overall_deadline
    transitioned = False
    settled_reads = 0
    settled_identity: tuple[str, int, str] | None = None
    reads = 0
    same_surface_after_transition = False
    required = max(2, int(settled_reads_required))
    read_limit = max(required, int(max_successor_reads))
    other_popups = BLOCKER_CATEGORIES - {
        "regional_ads_consent",
        "unknown_blocker",
    }
    before_identity = (
        str(before.get("document_epoch") or ""),
        str(before.get("fingerprint") or ""),
    )

    while reads < read_limit and time.monotonic() < overall_deadline:
        after = inspect_fn(page)
        reads += 1
        now = time.monotonic()
        if not transitioned:
            if not _changed(before, after):
                if now >= transition_deadline:
                    return "transition_timeout", None, reads
                time.sleep(min(max(0.0, poll_interval), max(0.0, transition_deadline - now)))
                continue
            transitioned = True
            successor_deadline = min(
                overall_deadline,
                now + max(0.0, float(successor_grace)),
            )

        category = str(after.get("category") or "")
        if (
            after.get("authenticated_surface")
            or after.get("login_surface")
            or after.get("two_factor_surface")
            or category in {"checkpoint", "restriction", "suspended"}
            or category in other_popups
        ):
            return "completed", after, reads
        if after.get("present") and category == "regional_ads_consent":
            after_identity = (
                str(after.get("document_epoch") or ""),
                str(after.get("fingerprint") or ""),
            )
            # React may mutate or remount nodes while the same confirmation
            # remains visible. That is neither a successor nor permission to
            # click the same fingerprint again; keep waiting for authoritative
            # identity change or a terminal surface.
            if after_identity != before_identity:
                return "next_step", after, reads
            same_surface_after_transition = True
            settled_identity = None
            settled_reads = 0

        loading = bool(
            category == "operation_processing"
            or after.get("loading")
            or after.get("progress")
        )
        if not after.get("present") and not loading:
            identity = (
                str(after.get("document_epoch") or ""),
                max(0, int(after.get("mutation_epoch") or 0)),
                str(after.get("document_category") or ""),
            )
            if identity == settled_identity:
                settled_reads += 1
            else:
                settled_identity = identity
                settled_reads = 1
            if settled_reads >= required and now >= successor_deadline:
                return "completed", after, reads
        else:
            settled_identity = None
            settled_reads = 0

        if now >= successor_deadline:
            if same_surface_after_transition:
                return "loop", after, reads
            return "successor_timeout", None, reads
        time.sleep(min(max(0.0, poll_interval), max(0.0, successor_deadline - now)))

    if same_surface_after_transition:
        return "loop", after, reads
    return "successor_timeout", None, reads


def resolve_regional_ads_consent(
    page: Any,
    *,
    max_transitions: int = MAX_ADS_TRANSITIONS,
    transition_timeout: float = 15.0,
    successor_grace: float = 6.0,
    overall_timeout: float = 120.0,
    settled_reads_required: int = ADS_SUCCESSOR_SETTLED_READS,
    max_successor_reads: int = MAX_ADS_SUCCESSOR_READS,
    poll_interval: float = 0.1,
    max_action_retries: int = MAX_CONSENT_ACTION_RETRIES,
    inspect_fn: Callable[[Any], dict[str, Any]] = inspect_topmost_blocker,
    action_fn: Callable[[Any, dict[str, Any], str], dict[str, Any]] = (
        perform_fresh_action
    ),
    on_transition: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    on_action: Callable[[dict[str, Any], str], None] | None = None,
) -> dict[str, Any]:
    """Resolve one regional ads-consent wizard without retaining stale state."""
    observed = inspect_fn(page)
    if observed.get("document_category") == "browser_internal_error":
        return {"handled": False, "ok": False, "step": "browser_internal_error"}
    if observed.get("category") != "regional_ads_consent":
        return {"handled": False, "ok": True, "step": "not_present"}
    seen: set[tuple[str, str]] = set()
    transitions = 0
    overall_deadline = time.monotonic() + max(0.0, float(overall_timeout))
    while transitions < max(1, int(max_transitions)):
        if time.monotonic() >= overall_deadline:
            return {
                "handled": transitions > 0,
                "ok": False,
                "step": "ads_consent_transition_timeout",
                "timeout_phase": "overall",
                "transitions": transitions,
            }
        identity = (
            str(observed.get("document_epoch") or ""),
            str(observed.get("fingerprint") or ""),
        )
        if identity in seen:
            return {
                "handled": transitions > 0,
                "ok": False,
                "step": "ads_consent_loop_detected",
                "transitions": transitions,
            }
        dispatched: dict[str, Any] = {"ok": False, "reason": "action_unavailable"}
        action = ""
        for _retry in range(max(1, int(max_action_retries))):
            action = str(observed.get("recommended_action") or "")
            if action not in REGIONAL_ADS_ACTIONS:
                dispatched = {"ok": False, "reason": "action_not_allowed"}
            else:
                dispatched = action_fn(page, observed, action)
            if dispatched.get("ok"):
                break
            fresh = inspect_fn(page)
            if fresh.get("category") != "regional_ads_consent":
                return {
                    "handled": transitions > 0,
                    "ok": True,
                    "step": "completed",
                    "transitions": transitions,
                    "next_category": str(fresh.get("category") or ""),
                }
            observed = fresh
            if time.monotonic() >= overall_deadline:
                break
            time.sleep(min(max(0.0, poll_interval), 0.1))
        if not dispatched.get("ok"):
            vision_result = _attempt_vision_fallback(
                page, action, str(dispatched.get("reason") or ""),
            )
            if vision_result.get("ok"):
                dispatched = vision_result
        if not dispatched.get("ok"):
            return {
                "handled": transitions > 0,
                "ok": False,
                "step": "ads_consent_action_unavailable",
                "action_reason": str(dispatched.get("reason") or "")[:80],
                "transitions": transitions,
            }
        identity = (
            str(observed.get("document_epoch") or ""),
            str(observed.get("fingerprint") or ""),
        )
        if identity in seen:
            return {
                "handled": transitions > 0,
                "ok": False,
                "step": "ads_consent_loop_detected",
                "transitions": transitions,
            }
        seen.add(identity)
        if on_action is not None:
            try:
                on_action(observed, action)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        transitions += 1
        successor, after, successor_reads = _wait_for_ads_successor(
            page,
            observed,
            transition_timeout=transition_timeout,
            successor_grace=successor_grace,
            settled_reads_required=settled_reads_required,
            max_successor_reads=max_successor_reads,
            overall_deadline=overall_deadline,
            poll_interval=poll_interval,
            inspect_fn=inspect_fn,
        )
        if successor in {"transition_timeout", "successor_timeout"}:
            return {
                "handled": True,
                "ok": False,
                "step": "ads_consent_transition_timeout",
                "timeout_phase": (
                    "transition" if successor == "transition_timeout" else "successor"
                ),
                "transitions": transitions,
                "successor_reads": successor_reads,
            }
        if successor == "loop":
            return {
                "handled": True,
                "ok": False,
                "step": "ads_consent_loop_detected",
                "transitions": transitions,
                "successor_reads": successor_reads,
            }
        assert after is not None
        if on_transition is not None:
            try:
                on_transition(observed, after)
            except Exception as _exc:
                logger.debug("%s: %s", type(_exc).__name__, _exc)
                pass
        if successor == "completed":
            return {
                "handled": True,
                "ok": True,
                "step": "completed",
                "transitions": transitions,
                "successor_reads": successor_reads,
                "next_category": str(after.get("category") or ""),
            }
        observed = after
    return {
        "handled": True,
        "ok": False,
        "step": "ads_consent_loop_detected",
        "limit_reason": "step_limit",
        "transitions": transitions,
    }


def _emit_consent_event(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event: str,
    **payload: Any,
) -> None:
    if callback is None:
        return
    safe = {
        str(key)[:80]: (
            value
            if isinstance(value, (bool, int, float))
            else str(value or "")[:80]
        )
        for key, value in payload.items()
    }
    try:
        callback(str(event)[:80], safe)
    except Exception as _exc:
        logger.debug("%s: %s", type(_exc).__name__, _exc)
        pass


def _terminal_consent_successor(observed: dict[str, Any]) -> bool:
    category = str(observed.get("category") or "")
    return bool(
        observed.get("authenticated_surface")
        or observed.get("login_surface")
        or observed.get("two_factor_surface")
        or category in {
            "checkpoint",
            "restriction",
            "suspended",
            "save_login_info",
            "notifications_prompt",
            "promo_or_ad",
            "open_in_app",
        }
    )


def _wait_for_cookie_successor(
    page: Any,
    before: dict[str, Any],
    *,
    deadline: float,
    transition_timeout: float,
    max_reads: int,
    settled_reads_required: int,
    poll_interval: float,
    inspect_fn: Callable[[Any], dict[str, Any]],
    surface_category: str = "cookie_consent",
) -> tuple[dict[str, Any] | None, int]:
    """Wait through a same-surface remount/loading gap for a fresh successor."""
    local_deadline = min(
        deadline, time.monotonic() + max(0.0, float(transition_timeout))
    )
    before_identity = (
        str(before.get("document_epoch") or ""),
        str(before.get("fingerprint") or ""),
    )
    settled_identity: tuple[str, int, str] | None = None
    settled_reads = 0
    reads = 0
    required = max(2, int(settled_reads_required))
    while reads < max(required, int(max_reads)) and time.monotonic() < local_deadline:
        after = inspect_fn(page)
        reads += 1
        category = str(after.get("category") or "")
        identity = (
            str(after.get("document_epoch") or ""),
            str(after.get("fingerprint") or ""),
        )
        if _terminal_consent_successor(after):
            return after, reads
        if after.get("present"):
            if category != surface_category:
                return after, reads
            # A successful cookie click commonly remounts the same typed
            # surface for a few reads while the login/ads successor is being
            # committed. A new DOM identity alone must not end the wait or
            # authorize another click on the disappearing cookie manager.
            settled_identity = None
            settled_reads = 0
        elif category != "operation_processing" and not after.get("loading"):
            stable_identity = (
                str(after.get("document_epoch") or ""),
                max(0, int(after.get("mutation_epoch") or 0)),
                str(after.get("document_category") or ""),
            )
            if stable_identity == settled_identity:
                settled_reads += 1
            else:
                settled_identity = stable_identity
                settled_reads = 1
            if settled_reads >= required:
                return after, reads
        time.sleep(min(max(0.0, poll_interval), max(0.0, local_deadline - time.monotonic())))
    return None, reads


def resolve_typed_consent_chain(
    page: Any,
    *,
    max_steps: int = MAX_CONSENT_CHAIN_STEPS,
    overall_timeout: float = 120.0,
    transition_timeout: float = 8.0,
    max_action_retries: int = MAX_CONSENT_ACTION_RETRIES,
    max_transition_reads: int = MAX_ADS_SUCCESSOR_READS,
    settled_reads_required: int = ADS_SUCCESSOR_SETTLED_READS,
    poll_interval: float = 0.1,
    inspect_fn: Callable[[Any], dict[str, Any]] = inspect_topmost_blocker,
    action_fn: Callable[[Any, dict[str, Any], str], dict[str, Any]] = (
        perform_fresh_action
    ),
    event_fn: Callable[[str, dict[str, Any]], None] | None = None,
    human: Any = None,
) -> dict[str, Any]:
    """Resolve a bounded cookie -> regional-ads typed consent sequence."""
    deadline = time.monotonic() + max(0.0, float(overall_timeout))
    seen: set[tuple[str, str]] = set()
    steps = 0
    handled = False

    def dispatch(current: dict[str, Any], action: str) -> dict[str, Any]:
        if action_fn is perform_fresh_action:
            return perform_fresh_action(
                page,
                current,
                action,
                human=human,
                event_fn=event_fn,
            )
        return action_fn(page, current, action)

    while steps < max(1, int(max_steps)) and time.monotonic() < deadline:
        observed = inspect_fn(page)
        category = str(observed.get("category") or "")
        _emit_consent_event(
            event_fn,
            "consent_surface_classified",
            category=category or "none",
            cookie_variant=observed.get("cookie_variant") or "",
            ads_step=observed.get("ads_step") or "",
        )
        if observed.get("document_category") == "browser_internal_error":
            _emit_consent_event(
                event_fn, "consent_chain_failed", reason="browser_internal_error"
            )
            return {"handled": handled, "ok": False, "step": "browser_internal_error"}
        if _terminal_consent_successor(observed):
            _emit_consent_event(
                event_fn,
                "consent_chain_completed",
                reason=category or "terminal_surface",
                steps=steps,
            )
            return {
                "handled": handled,
                "ok": True,
                "step": "completed",
                "transitions": steps,
                "next_category": category,
            }
        if category == "request_processing":
            identity = (
                str(observed.get("document_epoch") or ""),
                str(observed.get("fingerprint") or ""),
            )
            if identity in seen:
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="request_processing_loop_detected",
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "request_processing_loop_detected",
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            dispatched: dict[str, Any] = {
                "ok": False,
                "reason": "action_unavailable",
            }
            action = ""
            reclassified = False
            for _retry in range(max(1, int(max_action_retries))):
                action = str(observed.get("recommended_action") or "")
                if action in REQUEST_PROCESSING_ACTIONS:
                    dispatched = dispatch(observed, action)
                else:
                    dispatched = {"ok": False, "reason": "action_not_allowed"}
                if dispatched.get("ok"):
                    break
                fresh = inspect_fn(page)
                _emit_consent_event(
                    event_fn,
                    "consent_surface_reclassified",
                    category=fresh.get("category") or "none",
                    reason=dispatched.get("reason") or "action_retry",
                )
                if fresh.get("category") != "request_processing":
                    reclassified = True
                    break
                observed = fresh
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(max(0.0, poll_interval), 0.1))
            if reclassified:
                continue
            if not dispatched.get("ok"):
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="request_processing_action_unavailable",
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "request_processing_action_unavailable",
                    "action_reason": str(dispatched.get("reason") or "")[:80],
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            seen.add(identity)
            handled = True
            steps += 1
            _emit_consent_event(
                event_fn,
                "consent_action_dispatched",
                category="request_processing",
                action=action,
            )
            after, reads = _wait_for_cookie_successor(
                page,
                observed,
                deadline=deadline,
                transition_timeout=transition_timeout,
                max_reads=max_transition_reads,
                settled_reads_required=settled_reads_required,
                poll_interval=poll_interval,
                inspect_fn=inspect_fn,
                surface_category="request_processing",
            )
            if after is None:
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="request_processing_transition_timeout",
                )
                return {
                    "handled": True,
                    "ok": False,
                    "step": "request_processing_transition_timeout",
                    "transition_reads": reads,
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            _emit_consent_event(
                event_fn,
                "consent_transition_observed",
                before="request_processing",
                after=after.get("category") or "none",
            )
            _emit_consent_event(
                event_fn,
                "consent_surface_reclassified",
                category=after.get("category") or "none",
                reason="request_processing_transition",
            )
            continue
        if category == "cookie_consent":
            variant = str(observed.get("cookie_variant") or "cookie_dialog_legacy")
            _emit_consent_event(
                event_fn, "cookie_variant_selected", variant=variant
            )
            identity = (
                str(observed.get("document_epoch") or ""),
                str(observed.get("fingerprint") or ""),
            )
            if identity in seen:
                _emit_consent_event(
                    event_fn, "consent_chain_failed", reason="cookie_consent_loop_detected"
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "cookie_consent_loop_detected",
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            dispatched: dict[str, Any] = {"ok": False, "reason": "action_unavailable"}
            action = ""
            reclassified = False
            for _retry in range(max(1, int(max_action_retries))):
                action = str(observed.get("recommended_action") or "")
                if action in COOKIE_ACTIONS:
                    dispatched = dispatch(observed, action)
                else:
                    dispatched = {"ok": False, "reason": "action_not_allowed"}
                if dispatched.get("ok"):
                    break
                fresh = inspect_fn(page)
                _emit_consent_event(
                    event_fn,
                    "consent_surface_reclassified",
                    category=fresh.get("category") or "none",
                    reason=dispatched.get("reason") or "action_retry",
                )
                if fresh.get("category") != "cookie_consent":
                    reclassified = True
                    break
                observed = fresh
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(max(0.0, poll_interval), 0.1))
            if reclassified:
                continue
            if not dispatched.get("ok"):
                vision_result = _attempt_vision_fallback(
                    page,
                    action,
                    str(dispatched.get("reason") or ""),
                    event_fn=event_fn,
                )
                if vision_result.get("ok"):
                    dispatched = vision_result
            if not dispatched.get("ok"):
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="cookie_consent_action_unavailable",
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "cookie_consent_action_unavailable",
                    "action_reason": str(dispatched.get("reason") or "")[:80],
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            identity = (
                str(observed.get("document_epoch") or ""),
                str(observed.get("fingerprint") or ""),
            )
            if identity in seen:
                _emit_consent_event(
                    event_fn, "consent_chain_failed", reason="cookie_consent_loop_detected"
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "cookie_consent_loop_detected",
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            seen.add(identity)
            handled = True
            steps += 1
            _emit_consent_event(
                event_fn,
                "consent_action_dispatched",
                category="cookie_consent",
                action=action,
            )
            after, reads = _wait_for_cookie_successor(
                page,
                observed,
                deadline=deadline,
                transition_timeout=transition_timeout,
                max_reads=max_transition_reads,
                settled_reads_required=settled_reads_required,
                poll_interval=poll_interval,
                inspect_fn=inspect_fn,
            )
            if after is None:
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="cookie_consent_transition_timeout",
                )
                return {
                    "handled": True,
                    "ok": False,
                    "step": "cookie_consent_transition_timeout",
                    "transition_reads": reads,
                    "manual_required": True,
                    "consent_state": "consent_pending",
                }
            _emit_consent_event(
                event_fn,
                "consent_transition_observed",
                before="cookie_consent",
                after=after.get("category") or "none",
            )
            _emit_consent_event(
                event_fn,
                "consent_surface_reclassified",
                category=after.get("category") or "none",
                reason="cookie_transition",
            )
            continue
        if category == "regional_ads_consent":
            _emit_consent_event(
                event_fn,
                "regional_ads_step_selected",
                step=observed.get("ads_step") or "unknown_ads_step",
            )

            def on_ads_action(current: dict[str, Any], action: str) -> None:
                _emit_consent_event(
                    event_fn,
                    "consent_action_dispatched",
                    category="regional_ads_consent",
                    action=action,
                )

            def on_ads_transition(
                before: dict[str, Any], after: dict[str, Any]
            ) -> None:
                _emit_consent_event(
                    event_fn,
                    "consent_transition_observed",
                    before=before.get("ads_step") or "regional_ads_consent",
                    after=after.get("ads_step") or after.get("category") or "none",
                )
                _emit_consent_event(
                    event_fn,
                    "consent_surface_reclassified",
                    category=after.get("category") or "none",
                    reason="regional_ads_transition",
                )

            result = resolve_regional_ads_consent(
                page,
                max_transitions=max(1, int(max_steps) - steps),
                transition_timeout=min(
                    max(0.0, float(transition_timeout)),
                    max(0.0, deadline - time.monotonic()),
                ),
                overall_timeout=max(0.0, deadline - time.monotonic()),
                settled_reads_required=settled_reads_required,
                max_successor_reads=max_transition_reads,
                poll_interval=poll_interval,
                max_action_retries=max_action_retries,
                inspect_fn=inspect_fn,
                action_fn=lambda _page, current, action: dispatch(current, action),
                on_transition=on_ads_transition,
                on_action=on_ads_action,
            )
            handled = handled or bool(result.get("handled"))
            steps += max(0, int(result.get("transitions") or 0))
            if not result.get("ok"):
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason=result.get("step") or "regional_ads_failed",
                )
                result["manual_required"] = True
                result["consent_state"] = "consent_pending"
                return result
            continue
        if category == "operation_processing" or observed.get("loading") or observed.get("progress"):
            fresh = observed
            for _read in range(max(1, int(max_transition_reads))):
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(max(0.0, poll_interval), 0.1))
                fresh = inspect_fn(page)
                fresh_category = str(fresh.get("category") or "")
                if not (
                    fresh_category == "operation_processing"
                    or fresh.get("loading")
                    or fresh.get("progress")
                ):
                    _emit_consent_event(
                        event_fn,
                        "consent_surface_reclassified",
                        category=fresh_category or "none",
                        reason="transient_loading",
                    )
                    break
            else:
                fresh = observed
            fresh_category = str(fresh.get("category") or "")
            if (
                fresh_category == "operation_processing"
                or fresh.get("loading")
                or fresh.get("progress")
            ):
                _emit_consent_event(
                    event_fn,
                    "consent_chain_failed",
                    reason="consent_chain_timeout",
                )
                return {
                    "handled": handled,
                    "ok": False,
                    "step": "consent_chain_timeout",
                    "manual_required": True,
                    "consent_state": "consent_pending",
                    "transitions": steps,
                }
            continue
        if observed.get("present") or observed.get("unrecognized_surface"):
            _emit_consent_event(
                event_fn, "consent_chain_failed", reason="unrecognized_surface"
            )
            return {
                "handled": handled,
                "ok": False,
                "step": "unrecognized_surface",
                "manual_required": True,
                "consent_state": "consent_pending",
            }
        if not handled:
            return {"handled": False, "ok": True, "step": "not_present"}
        _emit_consent_event(
            event_fn, "consent_chain_completed", reason="settled_absence", steps=steps
        )
        return {
            "handled": True,
            "ok": True,
            "step": "completed",
            "transitions": steps,
        }

    reason = (
        "consent_chain_step_limit"
        if steps >= max(1, int(max_steps))
        else "consent_chain_timeout"
    )
    _emit_consent_event(event_fn, "consent_chain_failed", reason=reason)
    return {
        "handled": handled,
        "ok": False,
        "step": reason,
        "manual_required": True,
        "consent_state": "consent_pending",
        "transitions": steps,
    }
