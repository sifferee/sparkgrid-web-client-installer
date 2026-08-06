from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from automation_plans import ensure_automation_schema, mark_slot, stable_login_delay_minutes


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPARKGRID_DATA_DIR") or ROOT / "data").resolve()
DB_PATH = DATA_DIR / "bot.db"


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def log(message: str, level: str = "INFO") -> None:
    from log_config import log_to_file_and_print
    log_to_file_and_print("automation", message, level)


def _parse_utc(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def slot_payload(conn: sqlite3.Connection, slot_id: int) -> dict[str, Any] | None:
    ensure_automation_schema(conn)
    row = conn.execute(
        """
        SELECT s.*,p.name AS plan_name,p.enabled,p.engine,p.provider,
               p.login_delay_min_minutes,p.login_delay_max_minutes,
               COALESCE(a.status,'') AS account_status,
               COALESCE(a.web_upload_login_status,'') AS login_status,
               COALESCE(a.web_upload_last_login_at,'') AS last_login_at,
               COALESCE(a.web_upload_last_error,'') AS account_error
        FROM automation_plan_slots s
        JOIN automation_plans p ON p.id=s.plan_id
        JOIN accounts a ON a.name=s.account_name
        WHERE s.id=?
        """,
        (int(slot_id),),
    ).fetchone()
    return dict(row) if row else None


def _latest_job_status(conn: sqlite3.Connection, account: str, started_at: str) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT status,COALESCE(last_error,'') AS last_error,COALESCE(current_step,'') AS current_step
        FROM ig_web_upload_jobs
        WHERE account_name=? AND datetime(created_at)>=datetime(?)
        ORDER BY id DESC LIMIT 1
        """,
        (account, started_at),
    ).fetchone()
    if not row:
        return "failed", "automation worker finished without creating a publication job"
    status = str(row["status"] or "").lower()
    detail = str(row["last_error"] or row["current_step"] or status)
    if status == "success":
        return "success", ""
    if status in {"no_content", "cooldown"}:
        return "no_content", detail
    return "failed", detail


def run(slot_id: int) -> int:
    conn = db_conn()
    try:
        slot = slot_payload(conn, slot_id)
        if not slot:
            log(f"Automation slot #{slot_id} not found", "ERROR")
            return 2
        if not bool(int(slot.get("enabled") or 0)):
            mark_slot(conn, slot_id, "skipped", "automation plan is paused")
            return 0
        state = " ".join(str(slot.get(key) or "") for key in ("account_status", "login_status", "account_error")).lower()
        if any(marker in state for marker in ("suspend", "banned", "account disabled")):
            mark_slot(conn, slot_id, "skipped", "suspended account")
            return 0
        if "logged_in" not in str(slot.get("login_status") or "").lower():
            mark_slot(conn, slot_id, "skipped", "login required before automation can run")
            return 0
        now = datetime.now(timezone.utc)
        window_end = _parse_utc(str(slot.get("window_end") or ""))
        if window_end and now > window_end:
            mark_slot(conn, slot_id, "missed", "scheduled window ended before the account became available")
            return 0
        login_at = _parse_utc(str(slot.get("last_login_at") or ""))
        if not login_at:
            # Existing logged-in accounts receive a real baseline once. Future
            # successful login workflows update this field directly.
            login_at = now
            conn.execute(
                "UPDATE accounts SET web_upload_last_login_at=?,updated_at=datetime('now') WHERE name=?",
                (login_at.strftime("%Y-%m-%d %H:%M:%S"), str(slot["account_name"])),
            )
            conn.commit()
        delay = stable_login_delay_minutes(slot, str(slot["account_name"]), login_at.isoformat())
        eligible_at = login_at + timedelta(minutes=delay)
        if now < eligible_at:
            if window_end and eligible_at > window_end:
                mark_slot(conn, slot_id, "missed", f"login wait ({delay}m) ends after this window")
            else:
                conn.execute(
                    """
                    UPDATE automation_plan_slots
                    SET status='waiting_login_delay',scheduled_for=?,last_error=?,updated_at=datetime('now')
                    WHERE id=?
                    """,
                    (
                        eligible_at.strftime("%Y-%m-%d %H:%M:%S"),
                        f"waiting {delay} minutes after login",
                        int(slot_id),
                    ),
                )
                conn.commit()
            return 0
        mark_slot(conn, slot_id, "running")
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    finally:
        conn.close()

    operation = str(slot.get("engine") or "clean_web")
    if operation not in {"clean_web", "api"}:
        operation = "clean_web"
    command = [
        sys.executable, "-u", str(ROOT / "connection_scheduler.py"),
        "--operation", operation,
        "--accounts", str(slot["account_name"]),
        "--parallel", "1",
        "--provider", str(slot.get("provider") or "camoufox"),
        "--ignore-cooldown",
        "--target", "1",
        "--pre-warmup-min", "1",
        "--pre-warmup-max", "2",
        "--post-warmup-min", "1",
        "--post-warmup-max", "3",
    ]
    env = os.environ.copy()
    env["SPARKGRID_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log(f"{slot['plan_name']}: starting scheduled session for {slot['account_name']}", "OK")
    completed = subprocess.run(command, cwd=str(ROOT), env=env, check=False)

    conn = db_conn()
    try:
        final_status, detail = _latest_job_status(conn, str(slot["account_name"]), started_at)
        if completed.returncode != 0 and final_status == "success":
            log(
                "Connection scheduler infrastructure exit "
                f"{completed.returncode} did not revoke persisted workflow "
                "success",
                "WARNING",
            )
        mark_slot(conn, slot_id, final_status, detail)
    finally:
        conn.close()
    log(
        f"{slot['plan_name']}: {slot['account_name']} -> {final_status}{(': ' + detail) if detail else ''}",
        "OK" if final_status == "success" else "WARNING",
    )
    return 0 if final_status in {"success", "no_content"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one persisted SparkGrid Automation Plan slot")
    parser.add_argument("--slot-id", type=int, required=True)
    args = parser.parse_args()
    return run(int(args.slot_id))


if __name__ == "__main__":
    raise SystemExit(main())
