from __future__ import annotations

import base64
import ctypes
import hashlib
import html
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


APP_VERSION = "2.20.14-beta.1"
LICENSE_API = "https://66.42.82.20/v1"
APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_ROOT = Path(os.environ.get("SPARKGRID_DATA_DIR") or Path(os.environ["LOCALAPPDATA"]) / "SparkGrid-WebClient" / "data")
LICENSE_DIR = DATA_ROOT / "license"
CACHE_FILE = LICENSE_DIR / "license.dat"
PUBLIC_KEY_FILE = APP_ROOT / "license" / "signing_public.pem"
CA_FILE = APP_ROOT / "license" / "tls_ca.crt"
ONLINE_INTERVAL = 12 * 60 * 60


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    result = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "SparkGrid License", None, None, None, 0x1, ctypes.byref(result)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    result = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def device_id() -> str:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
        machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    root = (Path(os.environ.get("SystemRoot", r"C:\Windows")).anchor or "C:\\")
    serial = wintypes.DWORD()
    ctypes.windll.kernel32.GetVolumeInformationW(
        root, None, 0, ctypes.byref(serial), None, None, None, 0
    )
    material = f"SparkGrid-License-v1|{machine_guid}|{serial.value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_token(token: str) -> dict:
    encoded, signature = token.split(".", 1)
    raw = _b64decode(encoded)
    public_key = serialization.load_pem_public_key(PUBLIC_KEY_FILE.read_bytes())
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("Unexpected license public key")
    public_key.verify(_b64decode(signature), raw)
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("iss") != "sparkgrid-license":
        raise ValueError("Unexpected license issuer")
    if payload.get("device_id") != device_id():
        raise ValueError("License belongs to another device")
    if int(payload.get("valid_until") or 0) <= int(time.time()):
        raise ValueError("License offline period expired")
    return payload


def load_cache() -> dict | None:
    try:
        return json.loads(_dpapi_unprotect(CACHE_FILE.read_bytes()).decode("utf-8"))
    except Exception:
        return None


def save_cache(value: dict) -> None:
    LICENSE_DIR.mkdir(parents=True, exist_ok=True)
    protected = _dpapi_protect(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    temporary = CACHE_FILE.with_suffix(".tmp")
    temporary.write_bytes(protected)
    os.replace(temporary, CACHE_FILE)


def _request(endpoint: str, license_key: str) -> dict:
    body = json.dumps(
        {"license_key": license_key, "device_id": device_id(), "app_version": APP_VERSION}
    ).encode("utf-8")
    request = urllib.request.Request(
        LICENSE_API + endpoint,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": f"SparkGrid/{APP_VERSION}"},
        method="POST",
    )
    context = ssl.create_default_context(cafile=str(CA_FILE))
    with urllib.request.urlopen(request, timeout=7, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def activate_and_save(license_key: str) -> tuple[bool, str]:
    key = license_key.strip().upper()
    if not key:
        return False, "Введите лицензионный ключ"
    try:
        result = _request("/activate", key)
    except Exception:
        return False, "Сервер лицензий недоступен. Проверьте интернет и повторите."
    if not result.get("ok"):
        return False, str(result.get("message") or "Активация отклонена")
    try:
        payload = verify_token(str(result["token"]))
        save_cache(
            {"license_key": key, "token": result["token"], "last_online": int(time.time())}
        )
    except Exception:
        return False, "Сервер вернул некорректную подпись лицензии"
    customer = str(payload.get("customer") or "клиент")
    return True, f"Лицензия активирована: {customer}"


def _cached_license_valid() -> bool:
    cached = load_cache()
    if not cached:
        return False
    try:
        verify_token(str(cached.get("token") or ""))
    except Exception:
        return False
    if int(time.time()) - int(cached.get("last_online") or 0) < ONLINE_INTERVAL:
        return True
    try:
        result = _request("/refresh", str(cached.get("license_key") or ""))
    except Exception:
        return True
    if not result.get("ok"):
        try:
            CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    try:
        verify_token(str(result["token"]))
        save_cache(
            {"license_key": cached["license_key"], "token": result["token"], "last_online": int(time.time())}
        )
        return True
    except Exception:
        return False


def _page(message: str = "", success: bool = False) -> bytes:
    color = "#4ade80" if success else "#f87171"
    notice = f'<div class="notice" style="color:{color}">{html.escape(message)}</div>' if message else ""
    disabled = "disabled" if success else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SparkGrid License</title><style>
body{{margin:0;background:#07101f;color:#e5eefc;font:16px system-ui;display:grid;place-items:center;min-height:100vh}}.card{{width:min(520px,calc(100% - 40px));background:#101c2f;border:1px solid #29405e;border-radius:18px;padding:30px;box-shadow:0 30px 80px #0008}}h1{{margin:0 0 8px}}p{{color:#9fb0c8;line-height:1.5}}input{{box-sizing:border-box;width:100%;padding:14px;border-radius:10px;border:1px solid #3a526f;background:#07101f;color:#fff;font-size:17px;text-transform:uppercase}}button{{width:100%;margin-top:14px;padding:14px;border:0;border-radius:10px;background:#4f8cff;color:#fff;font-weight:700;font-size:16px;cursor:pointer}}button:disabled{{opacity:.5}}.id{{font:12px monospace;color:#71839c;word-break:break-all}}.notice{{margin:14px 0;font-weight:650}}</style></head><body><div class="card"><h1>SparkGrid</h1><p>Введите лицензионный ключ. Ключ будет привязан к этому компьютеру.</p>{notice}<form method="post" action="/activate"><input name="license_key" autocomplete="off" placeholder="SG-XXXXX-XXXXX-XXXXX-XXXXX" {disabled}><button {disabled}>Активировать</button></form><p class="id">Device ID: {device_id()}</p>{'<p>Можно закрыть эту вкладку — SparkGrid запускается.</p>' if success else ''}</div></body></html>""".encode("utf-8")


def _interactive_activation() -> bool:
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

        def send_page(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self.send_page(_page())

        def do_POST(self) -> None:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            ok, message = activate_and_save((fields.get("license_key") or [""])[0])
            self.send_page(_page(message, ok))
            if ok:
                completed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    webbrowser.open(url)
    completed.wait()
    server.shutdown()
    server.server_close()
    return True


def require_license() -> None:
    return
