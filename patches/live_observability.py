"""Additive observability for source-only live client runs.

Nothing in this module is active unless SPARKGRID_LIVE_RUN_ID is present.
Instrumentation records facts; it never schedules work, retries an operation,
changes a timeout, catches an application exception, or edits client state.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SECRET_KEYS = ("password", "secret", "token", "cookie", "authorization", "proxy")
_DML = re.compile(
    r"^\s*(?:(INSERT|REPLACE)\s+INTO|(UPDATE)\s+|(DELETE)\s+FROM)\s*[\"`\[]?([A-Za-z0-9_]+)",
    re.I,
)
_RUN_ID_RE = re.compile(r"^live-\d{8}T\d{6}Z-[0-9a-f]{7,12}$")
_ORIGINAL_CONNECT = sqlite3.connect
_ORIGINAL_POPEN = subprocess.Popen
_INSTALLED = False
_RECORDER: "LiveRecorder | None" = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if any(word in str(key).lower() for word in _SECRET_KEYS) else _safe(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _command_text(args: Any) -> str:
    command = args[0] if isinstance(args, tuple) and args else args
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _process_role(command: str) -> str:
    lower = command.lower()
    if "connection_scheduler.py" in lower:
        return "scheduler"
    if any(name in lower for name in ("instagram_web_", "web_warmup.py", "automation_worker.py")):
        return "child_worker"
    if any(name in lower for name in ("camoufox", "firefox", "chrome", "sparkbrowser")):
        return "browser"
    return "child_process"


class LiveRecorder:
    def __init__(self, data_dir: Path, run_id: str, commit: str, role: str = "server") -> None:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"invalid live run id: {run_id!r}")
        self.data_dir = Path(data_dir).resolve()
        self.run_id = run_id
        self.commit = commit
        self.role = role
        self.run_dir = self.data_dir / "diagnostics" / "live" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "task_logs").mkdir(exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.errors_path = self.run_dir / "errors.jsonl"
        self.task_logs_path = self.run_dir / "task_logs" / "index.jsonl"
        self.summary_path = self.run_dir / "process_summary.json"
        self.manifest_path = self.run_dir / "manifest.json"
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._processes: dict[int, dict[str, Any]] = {}
        self._process_lock = threading.Lock()
        self._last_successful_stage = "observability_initialized"
        self._terminal_result = "running"
        self._exit_code: int | None = None
        self._finished = False
        self._started_at = utc_now()
        for path in (self.events_path, self.errors_path, self.task_logs_path):
            path.touch(exist_ok=True)
        self._writer = threading.Thread(target=self._writer_loop, name="sparkgrid-live-writer", daemon=True)
        self._writer.start()
        for stream_name in ("SPARKGRID_LIVE_STDOUT", "SPARKGRID_LIVE_STDERR"):
            log_path = os.environ.get(stream_name)
            if log_path:
                self._queue.put(("task_logs", {
                    "timestamp": utc_now(), "run_id": self.run_id, "pid": os.getpid(),
                    "process_role": role, "log_path": log_path,
                }))
        self.emit("runtime_start", pid=os.getpid(), process_role=role, operation="source_runtime")

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": utc_now(),
            "run_id": self.run_id,
            "pid": os.getpid(),
            "process_role": self.role,
            "event": event,
            **_safe(fields),
        }
        self._queue.put(("errors" if event in {"exception", "request_exception"} else "events", payload))

    def successful_stage(self, stage: str) -> None:
        self._last_successful_stage = str(stage)
        self.emit("successful_stage", stage=stage)

    def track_process(self, proc: subprocess.Popen[Any], command: str, kwargs: dict[str, Any]) -> None:
        role = _process_role(command)
        environment = kwargs.get("env") or {}
        entry = {
            "pid": proc.pid,
            "parent_pid": os.getpid(),
            "role": role,
            "command": command[:1000],
            "started_at": utc_now(),
            "exit_code": None,
            "connection_id": environment.get("SPARKGRID_CONNECTION_ID") or environment.get("CONNECTION_ID"),
            "proxy_id": environment.get("SPARKGRID_PROXY_ID") or environment.get("PROXY_ID"),
        }
        stdout = kwargs.get("stdout")
        log_path = getattr(stdout, "name", None)
        if log_path and isinstance(log_path, str):
            entry["task_log"] = log_path
            self._queue.put(("task_logs", {
                "timestamp": utc_now(), "run_id": self.run_id, "pid": proc.pid,
                "process_role": role, "log_path": log_path,
            }))
        with self._process_lock:
            self._processes[proc.pid] = entry
        self.emit("process_start", **entry)

        def watch() -> None:
            while proc.poll() is None:
                time.sleep(0.5)
            with self._process_lock:
                current = self._processes.get(proc.pid, entry)
                current["exit_code"] = proc.returncode
                current["finished_at"] = utc_now()
            self.emit("process_exit", pid=proc.pid, process_role=role, exit_code=proc.returncode)

        threading.Thread(target=watch, name=f"sparkgrid-live-proc-{proc.pid}", daemon=True).start()

    def finish(self, result: str, exit_code: int | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._terminal_result = result
        self._exit_code = exit_code
        self.emit("runtime_finish", terminal_result=result, exit_code=exit_code)
        deadline = time.time() + 2.0
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.02)
        self._write_snapshots()

    def _append(self, path: Path, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            target, payload = item
            path = self.errors_path if target == "errors" else self.task_logs_path if target == "task_logs" else self.events_path
            try:
                self._append(path, payload)
                self._write_snapshots()
            except Exception:
                # Observability must never affect the client's result.
                pass

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _write_snapshots(self) -> None:
        with self._process_lock:
            processes = list(self._processes.values())
        processes.append({
            "pid": os.getpid(), "parent_pid": os.getppid(), "role": self.role,
            "started_at": self._started_at, "exit_code": None if self._terminal_result == "running" else self._exit_code,
        })
        self._write_json(self.summary_path, {
            "run_id": self.run_id,
            "updated_at": utc_now(),
            "terminal_result": self._terminal_result,
            "processes": processes,
        })
        self._write_json(self.manifest_path, {
            "schema": 1,
            "run_id": self.run_id,
            "commit": self.commit,
            "started_at": self._started_at,
            "updated_at": utc_now(),
            "pid": os.getpid(),
            "process_role": self.role,
            "last_successful_stage": self._last_successful_stage,
            "terminal_result": self._terminal_result,
            "files": {
                "events": "events.jsonl",
                "errors": "errors.jsonl",
                "task_logs": "task_logs/index.jsonl",
                "process_summary": "process_summary.json",
            },
        })


class LiveRequestMiddleware:
    """Pure ASGI observer that forwards every message unchanged."""

    def __init__(self, app: Any, recorder: LiveRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        body_parts: list[bytes] = []
        status_code = 500

        async def observed_receive() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "http.request" and message.get("body"):
                body_parts.append(message["body"])
            return message

        async def observed_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 0))
            await send(message)

        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            self.recorder.emit(
                "request_exception", operation=f"{method} {path}", backend_request=path,
                exception=type(exc).__name__, traceback="".join(traceback.format_exception(exc)),
            )
            raise
        finally:
            payload: Any = None
            raw = b"".join(body_parts)
            if raw and len(raw) <= 1_000_000:
                try:
                    payload = _safe(json.loads(raw.decode("utf-8")))
                except Exception:
                    payload = {"body_bytes": len(raw)}
            accounts: list[str] = []
            if isinstance(payload, dict):
                candidates = payload.get("accounts") or payload.get("account_names") or payload.get("account_name") or payload.get("account")
                if isinstance(candidates, list):
                    accounts = [str(item)[:120] for item in candidates[:100]]
                elif candidates:
                    accounts = [str(candidates)[:120]]
            self.recorder.emit(
                "backend_request", operation=f"{method} {path}", backend_request=path,
                account_id=accounts, request=payload, status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            if 200 <= status_code < 400:
                self.recorder.successful_stage(f"{method} {path}")


def _install_sql_trace(recorder: LiveRecorder) -> None:
    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = _ORIGINAL_CONNECT(*args, **kwargs)
        database = str(args[0] if args else kwargs.get("database", ""))

        def trace(statement: str) -> None:
            match = _DML.match(statement)
            if match:
                operation = next(group for group in match.groups()[:3] if group)
                recorder.emit(
                    "db_state_transition", operation=operation.upper(),
                    table=match.group(4), database=Path(database).name,
                )

        connection.set_trace_callback(trace)
        return connection

    sqlite3.connect = traced_connect  # type: ignore[assignment]


def _install_process_trace(recorder: LiveRecorder) -> None:
    def tracked_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        proc = _ORIGINAL_POPEN(*args, **kwargs)
        recorder.track_process(proc, _command_text(args), kwargs)
        return proc

    subprocess.Popen = tracked_popen  # type: ignore[assignment,misc]


def _install_exception_trace(recorder: LiveRecorder) -> None:
    original_sys_hook = sys.excepthook

    def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        recorder.emit("exception", exception=exc_type.__name__, message=str(exc), traceback="".join(traceback.format_exception(exc_type, exc, tb)))
        original_sys_hook(exc_type, exc, tb)

    sys.excepthook = sys_hook
    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def thread_hook(args: Any) -> None:
            recorder.emit("exception", thread=getattr(args.thread, "name", ""), exception=args.exc_type.__name__, message=str(args.exc_value), traceback="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
            original_thread_hook(args)

        threading.excepthook = thread_hook


def install_live_observability(app: Any, data_dir: Path, db_path: Path, root: Path) -> LiveRecorder:
    del db_path, root  # Paths are intentionally observed, never modified.
    global _INSTALLED, _RECORDER
    if _INSTALLED and _RECORDER is not None:
        return _RECORDER
    run_id = os.environ["SPARKGRID_LIVE_RUN_ID"]
    commit = os.environ.get("SPARKGRID_LIVE_COMMIT", "unknown")
    recorder = LiveRecorder(Path(data_dir), run_id, commit, role="server")
    _RECORDER = recorder
    _INSTALLED = True
    _install_sql_trace(recorder)
    _install_process_trace(recorder)
    _install_exception_trace(recorder)
    app.add_middleware(LiveRequestMiddleware, recorder=recorder)
    atexit.register(lambda: recorder.finish("process_exit", 0))
    return recorder
