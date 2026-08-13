#!/usr/bin/env python3
"""Run the official client source with additive live observability enabled."""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

import uvicorn

from live_observability import install_live_observability


def _configure_source_assets() -> None:
    import camoufox.locale as locale
    import camoufox.pkgman as pkgman

    pkgman.INSTALL_DIR = Path(os.environ["SPARKGRID_CAMOUFOX_DIR"]).resolve()
    locale.MMDB_FILE = Path(os.environ["SPARKGRID_GEOIP_PATH"]).resolve()


_configure_source_assets()
import app as official


def _instrumented_ui(source: Path, destination: Path) -> Path:
    """Add an invisible event observer without changing the visible UI."""
    html = source.read_text(encoding="utf-8")
    script = r"""
<script id="sparkgrid-live-observer">
(()=>{const endpoint='/__live_observability/ui-action';
const send=(event,element)=>{
  try{
    const handler=(element?.getAttribute?.('onclick')||'').match(/^\s*([A-Za-z_$][\w$]*)/)?.[1]||'';
    const row=element?.closest?.('[data-account],[data-account-name]');
    const account_id=row?.dataset?.account||row?.dataset?.accountName||'';
    const payload=JSON.stringify({event,tag:element?.tagName||'',id:element?.id||'',label:(element?.innerText||element?.getAttribute?.('aria-label')||'').trim().slice(0,120),handler,account_id});
    if(navigator.sendBeacon){navigator.sendBeacon(endpoint,new Blob([payload],{type:'application/json'}));}
    else{fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:payload,keepalive:true}).catch(()=>{});}
  }catch(_){}
};
document.addEventListener('click',e=>send('click',e.target?.closest?.('button,a,[role="button"]')||e.target),true);
document.addEventListener('change',e=>send('change',e.target),true);
document.addEventListener('submit',e=>send('submit',e.target),true);
})();
</script>
"""
    html = html.replace("</body>", script + "\n</body>", 1)
    destination.write_text(html, encoding="utf-8")
    return destination


def main() -> int:
    run_id = os.environ.get("SPARKGRID_LIVE_RUN_ID", "")
    if not re.fullmatch(r"live-\d{8}T\d{6}Z-[0-9a-f]{7,12}", run_id):
        print("SPARKGRID_LIVE_RUN_ID is missing or invalid", file=sys.stderr)
        return 2
    recorder = install_live_observability(official.app, official.DATA_DIR, official.DB_PATH, official.ROOT)
    official.UI_PATH = _instrumented_ui(official.UI_PATH, recorder.run_dir / "index.live.html")

    @official.app.post("/__live_observability/ui-action")
    async def live_ui_action(request: official.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        recorder.emit(
            "ui_action",
            operation=str(body.get("handler") or body.get("event") or "ui"),
            account_id=str(body.get("account_id") or ""),
            ui_action=body,
        )
        return {"ok": True}

    @official.app.get("/__live_observability/status")
    async def live_status() -> dict[str, Any]:
        return {"ok": True, "run_id": recorder.run_id, "pid": os.getpid()}

    official.ensure_schema()
    host = os.environ.get("WEB_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_UI_PORT", "8770"))
    recorder.successful_stage("schema_ready")
    try:
        uvicorn.run(official.app, host=host, port=port, log_level="info")
        recorder.finish("stopped", 0)
        return 0
    except BaseException as exc:
        recorder.emit("exception", exception=type(exc).__name__, message=str(exc), traceback="".join(traceback.format_exception(exc)))
        recorder.finish("failed", 1)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
