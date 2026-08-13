from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

VALID_AFTER_FINAL = {"repeat", "stop"}
VALID_STRATEGIES = {"standard", "custom"}
DEFAULT_POSTS_PER_RUN = 3


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    return False


def ensure_plan_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_account_content_plan_state (
            account_name TEXT PRIMARY KEY,
            current_set_order INTEGER NOT NULL DEFAULT 0,
            current_item_order INTEGER NOT NULL DEFAULT 0,
            after_final TEXT NOT NULL DEFAULT 'repeat',
            is_stopped INTEGER NOT NULL DEFAULT 0,
            strategy TEXT NOT NULL DEFAULT 'standard',
            posts_per_run INTEGER NOT NULL DEFAULT 3,
            standard_asset_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    _ensure_column(conn, "ig_account_content_plan_state", "current_item_order", "INTEGER NOT NULL DEFAULT 0")
    strategy_added = _ensure_column(conn, "ig_account_content_plan_state", "strategy", "TEXT NOT NULL DEFAULT 'standard'")
    _ensure_column(conn, "ig_account_content_plan_state", "posts_per_run", "INTEGER NOT NULL DEFAULT 3")
    _ensure_column(conn, "ig_account_content_plan_state", "standard_asset_id", "INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_account_content_plan_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_account_content_plan_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            set_id INTEGER NOT NULL,
            asset_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            caption_override TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_plan_sets_account_order "
        "ON ig_account_content_plan_sets(account_name, sort_order, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_plan_items_set_order "
        "ON ig_account_content_plan_items(set_id, sort_order, id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ig_web_upload_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ig_web_upload_settings(key,value)
        VALUES ('work_mode', COALESCE((SELECT value FROM ig_web_upload_settings WHERE key='upload_engine'),'clean_web'))
        """
    )
    # Existing v2.13 plans were intentionally custom. Accounts without a saved
    # plan remain on the simple Standard setup: one creative, three posts/run.
    if strategy_added:
        conn.execute(
            """
            UPDATE ig_account_content_plan_state
            SET strategy='custom'
            WHERE account_name IN (SELECT DISTINCT account_name FROM ig_account_content_plan_sets)
            """
        )


def _asset_allowed(conn: sqlite3.Connection, account_name: str, asset_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM api_content_assets WHERE id=? AND status IN ('ready','uploaded') "
        "AND COALESCE(content_kind,'scale')='scale' AND (account_name='' OR account_name=?)",
        (int(asset_id), account_name),
    ).fetchone()
    return bool(row)


def _preferred_scale_asset(conn: sqlite3.Connection, account_name: str, asset_id: int = 0) -> dict[str, Any] | None:
    if int(asset_id or 0):
        row = conn.execute(
            """
            SELECT id,account_name,file_path,original_name,caption,status,content_kind
            FROM api_content_assets
            WHERE id=? AND status IN ('ready','uploaded') AND COALESCE(content_kind,'scale')='scale'
              AND (account_name='' OR account_name=?)
            """,
            (int(asset_id), account_name),
        ).fetchone()
        if row:
            data = dict(row)
            data["asset_id"] = int(data["id"])
            data["exists"] = bool(data.get("file_path") and Path(str(data["file_path"])).is_file())
            return data
    row = conn.execute(
        """
        SELECT id,account_name,file_path,original_name,caption,status,content_kind
        FROM api_content_assets
        WHERE status IN ('ready','uploaded') AND COALESCE(content_kind,'scale')='scale'
          AND (account_name='' OR account_name=?)
        ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END,
                 CASE WHEN account_name=? THEN 0 ELSE 1 END, id
        LIMIT 1
        """,
        (account_name, account_name),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["asset_id"] = int(data["id"])
    data["exists"] = bool(data.get("file_path") and Path(str(data["file_path"])).is_file())
    return data


def save_scale_settings(
    conn: sqlite3.Connection,
    account_names: Iterable[str],
    *,
    strategy: str = "standard",
    posts_per_run: int = DEFAULT_POSTS_PER_RUN,
    standard_asset_id: int | None = None,
    preserve_asset: bool = True,
) -> dict[str, Any]:
    strategy = str(strategy or "standard").strip().lower()
    if strategy not in VALID_STRATEGIES:
        raise ValueError("strategy must be standard or custom")
    posts_per_run = max(1, min(int(posts_per_run or DEFAULT_POSTS_PER_RUN), 100))
    names = []
    for raw in account_names or []:
        name = str(raw or "").strip().lstrip("@")
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError("Select at least one account")
    existing = {str(r[0]) for r in conn.execute(
        f"SELECT name FROM accounts WHERE name IN ({','.join('?' for _ in names)})", names
    ).fetchall()}
    missing = [name for name in names if name not in existing]
    if missing:
        raise ValueError("account not found: " + ", ".join(missing[:3]))

    chosen_asset = int(standard_asset_id or 0)
    for name in names:
        if chosen_asset and not _asset_allowed(conn, name, chosen_asset):
            raise ValueError(f"content asset #{chosen_asset} is not available for {name}")
        current = conn.execute(
            "SELECT standard_asset_id FROM ig_account_content_plan_state WHERE account_name=?", (name,)
        ).fetchone()
        asset_value = int(current[0] or 0) if current and preserve_asset and standard_asset_id is None else chosen_asset
        conn.execute(
            """
            INSERT INTO ig_account_content_plan_state(
                account_name,current_set_order,after_final,is_stopped,strategy,posts_per_run,standard_asset_id,updated_at
            ) VALUES (?,0,'repeat',0,?,?,?,datetime('now'))
            ON CONFLICT(account_name) DO UPDATE SET
                strategy=excluded.strategy,
                current_item_order=0,
                posts_per_run=excluded.posts_per_run,
                standard_asset_id=excluded.standard_asset_id,
                is_stopped=0,
                updated_at=datetime('now')
            """,
            (name, strategy, posts_per_run, asset_value),
        )
    conn.commit()
    return {"accounts": names, "strategy": strategy, "posts_per_run": posts_per_run}


def save_plan(
    conn: sqlite3.Connection,
    account_name: str,
    sets: Iterable[dict[str, Any]],
    *,
    current_set_order: int = 0,
    after_final: str = "repeat",
) -> dict[str, Any]:
    account_name = str(account_name or "").strip().lstrip("@")
    if not account_name:
        raise ValueError("account_name is required")
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account_name,)).fetchone():
        raise ValueError(f"account not found: {account_name}")
    after_final = str(after_final or "repeat").strip().lower()
    if after_final not in VALID_AFTER_FINAL:
        after_final = "repeat"

    normalized: list[dict[str, Any]] = []
    for raw_set in list(sets or []):
        items: list[dict[str, Any]] = []
        for raw_item in list((raw_set or {}).get("items") or []):
            try:
                asset_id = int((raw_item or {}).get("asset_id") or 0)
            except Exception:
                asset_id = 0
            if not asset_id:
                continue
            if not _asset_allowed(conn, account_name, asset_id):
                raise ValueError(f"content asset #{asset_id} is not available for {account_name}")
            items.append({
                "asset_id": asset_id,
                "caption_override": str((raw_item or {}).get("caption_override") or ""),
            })
        if not items:
            continue
        normalized.append({
            "title": str((raw_set or {}).get("title") or f"Launch {len(normalized) + 1}").strip()[:120],
            "items": items,
        })

    old_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM ig_account_content_plan_sets WHERE account_name=?", (account_name,)
    ).fetchall()]
    if old_ids:
        placeholders = ",".join("?" for _ in old_ids)
        conn.execute(f"DELETE FROM ig_account_content_plan_items WHERE set_id IN ({placeholders})", old_ids)
    conn.execute("DELETE FROM ig_account_content_plan_sets WHERE account_name=?", (account_name,))

    for set_order, plan_set in enumerate(normalized):
        cur = conn.execute(
            "INSERT INTO ig_account_content_plan_sets(account_name,title,sort_order,updated_at) "
            "VALUES (?,?,?,datetime('now'))",
            (account_name, plan_set["title"], set_order),
        )
        set_id = int(cur.lastrowid)
        for item_order, item in enumerate(plan_set["items"]):
            conn.execute(
                "INSERT INTO ig_account_content_plan_items(set_id,asset_id,sort_order,caption_override,updated_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (set_id, item["asset_id"], item_order, item["caption_override"]),
            )

    max_order = max(0, len(normalized) - 1)
    current_set_order = max(0, min(int(current_set_order or 0), max_order)) if normalized else 0
    conn.execute(
        """
        INSERT INTO ig_account_content_plan_state(
            account_name,current_set_order,current_item_order,after_final,is_stopped,strategy,posts_per_run,standard_asset_id,updated_at
        ) VALUES (?,?,0,?,0,'custom',3,0,datetime('now'))
        ON CONFLICT(account_name) DO UPDATE SET
            current_set_order=excluded.current_set_order,
            current_item_order=0,
            after_final=excluded.after_final,
            is_stopped=0,
            strategy='custom',
            updated_at=datetime('now')
        """,
        (account_name, current_set_order, after_final),
    )
    conn.commit()
    return get_plan(conn, account_name)


def get_plan(conn: sqlite3.Connection, account_name: str) -> dict[str, Any]:
    account_name = str(account_name or "").strip().lstrip("@")
    state = conn.execute(
        """
        SELECT current_set_order,current_item_order,after_final,is_stopped,strategy,posts_per_run,standard_asset_id
        FROM ig_account_content_plan_state WHERE account_name=?
        """,
        (account_name,),
    ).fetchone()
    current_order = int(state["current_set_order"] or 0) if state else 0
    current_item_order = int(state["current_item_order"] or 0) if state else 0
    after_final = str(state["after_final"] or "repeat") if state else "repeat"
    is_stopped = bool(int(state["is_stopped"] or 0)) if state else False
    strategy = str(state["strategy"] or "standard") if state else "standard"
    if strategy not in VALID_STRATEGIES:
        strategy = "standard"
    posts_per_run = max(1, min(int(state["posts_per_run"] or DEFAULT_POSTS_PER_RUN), 100)) if state else DEFAULT_POSTS_PER_RUN
    standard_asset_id = int(state["standard_asset_id"] or 0) if state else 0

    set_rows = conn.execute(
        "SELECT id,title,sort_order FROM ig_account_content_plan_sets WHERE account_name=? ORDER BY sort_order,id",
        (account_name,),
    ).fetchall()
    sets: list[dict[str, Any]] = []
    for row in set_rows:
        item_rows = conn.execute(
            """
            SELECT i.id,i.asset_id,i.sort_order,i.caption_override,
                   a.account_name AS asset_account,a.file_path,a.original_name,a.caption,a.status,a.content_kind
            FROM ig_account_content_plan_items i
            LEFT JOIN api_content_assets a ON a.id=i.asset_id
            WHERE i.set_id=?
            ORDER BY i.sort_order,i.id
            """,
            (int(row["id"]),),
        ).fetchall()
        items = []
        for item in item_rows:
            data = dict(item)
            data["exists"] = bool(data.get("file_path") and Path(str(data["file_path"])).is_file())
            data["effective_caption"] = str(data.get("caption_override") or data.get("caption") or "")
            items.append(data)
        sets.append({
            "id": int(row["id"]),
            "title": str(row["title"] or f"Launch {len(sets) + 1}"),
            "sort_order": int(row["sort_order"] or 0),
            "items": items,
        })
    if sets and current_order >= len(sets):
        current_order = len(sets) - 1
    if sets:
        current_item_order = max(0, min(current_item_order, len(sets[current_order]["items"])))
    else:
        current_item_order = 0
    standard_asset = _preferred_scale_asset(conn, account_name, standard_asset_id)
    return {
        "account_name": account_name,
        "strategy": strategy,
        "posts_per_run": posts_per_run,
        "standard_asset_id": int(standard_asset.get("asset_id") or 0) if standard_asset else standard_asset_id,
        "standard_asset": standard_asset,
        "sets": sets,
        "current_set_order": current_order,
        "current_item_order": current_item_order,
        "after_final": after_final if after_final in VALID_AFTER_FINAL else "repeat",
        "is_stopped": is_stopped,
    }


def next_plan_set(conn: sqlite3.Connection, account_name: str) -> dict[str, Any] | None:
    plan = get_plan(conn, account_name)
    if plan["strategy"] == "standard":
        asset = plan.get("standard_asset")
        items = []
        if asset and asset.get("exists", True):
            for _ in range(int(plan["posts_per_run"])):
                item = dict(asset)
                item["id"] = 0
                item["asset_id"] = int(asset["asset_id"])
                item["caption_override"] = ""
                item["effective_caption"] = str(asset.get("caption") or "")
                items.append(item)
        return {
            "configured": True,
            "strategy": "standard",
            "stopped": False,
            "set_order": 0,
            "set_count": 1,
            "title": "Standard",
            "items": items,
            "after_final": "repeat",
            "posts_per_run": int(plan["posts_per_run"]),
        }

    sets = plan["sets"]
    if not sets:
        return {
            "configured": True,
            "strategy": "custom",
            "stopped": False,
            "set_order": 0,
            "set_count": 0,
            "title": "Content pattern",
            "items": [],
            "after_final": plan["after_final"],
        }
    if plan["is_stopped"]:
        return {
            "configured": True,
            "strategy": "custom",
            "stopped": True,
            "set_order": int(plan["current_set_order"]),
            "set_count": len(sets),
            "title": str(sets[int(plan["current_set_order"])]["title"]),
            "items": [],
            "after_final": plan["after_final"],
        }
    order = max(0, min(int(plan["current_set_order"]), len(sets) - 1))
    chosen = sets[order]
    item_order = max(0, min(int(plan.get("current_item_order") or 0), len(chosen["items"])))
    return {
        "configured": True,
        "strategy": "custom",
        "stopped": False,
        "set_id": int(chosen["id"]),
        "set_order": order,
        "set_count": len(sets),
        "title": str(chosen["title"] or f"Launch {order + 1}"),
        "items": list(chosen["items"])[item_order:],
        "all_items": list(chosen["items"]),
        "current_item_order": item_order,
        "completed_items": item_order,
        "set_items_total": len(chosen["items"]),
        "after_final": plan["after_final"],
    }


def advance_plan(conn: sqlite3.Connection, account_name: str) -> dict[str, Any]:
    plan = get_plan(conn, account_name)
    if plan["strategy"] == "standard":
        return {
            "advanced": True,
            "strategy": "standard",
            "previous_set_order": 0,
            "current_set_order": 0,
            "is_stopped": False,
            "after_final": "repeat",
        }
    sets = plan["sets"]
    if not sets:
        return {"advanced": False, "reason": "no_plan"}
    current = max(0, min(int(plan["current_set_order"]), len(sets) - 1))
    stopped = 0
    if current + 1 < len(sets):
        next_order = current + 1
    elif plan["after_final"] == "stop":
        next_order = current
        stopped = 1
    else:
        next_order = 0
    conn.execute(
        """
        UPDATE ig_account_content_plan_state
        SET current_set_order=?,current_item_order=0,is_stopped=?,updated_at=datetime('now')
        WHERE account_name=?
        """,
        (next_order, stopped, account_name),
    )
    conn.commit()
    return {
        "advanced": True,
        "strategy": "custom",
        "previous_set_order": current,
        "current_set_order": next_order,
        "is_stopped": bool(stopped),
        "after_final": plan["after_final"],
    }


def reset_plan_position(conn: sqlite3.Connection, account_name: str, set_order: int = 0) -> dict[str, Any]:
    plan = get_plan(conn, account_name)
    count = len(plan["sets"])
    order = max(0, min(int(set_order or 0), max(0, count - 1))) if count else 0
    conn.execute(
        """
        INSERT INTO ig_account_content_plan_state(
            account_name,current_set_order,current_item_order,after_final,is_stopped,strategy,posts_per_run,standard_asset_id,updated_at
        ) VALUES (?,?,0,?,0,?,?,?,datetime('now'))
        ON CONFLICT(account_name) DO UPDATE SET
            current_set_order=excluded.current_set_order,
            current_item_order=0,
            is_stopped=0,
            updated_at=datetime('now')
        """,
        (
            account_name,
            order,
            plan["after_final"],
            plan["strategy"],
            plan["posts_per_run"],
            plan["standard_asset_id"],
        ),
    )
    conn.commit()
    return get_plan(conn, account_name)



def scale_library(conn: sqlite3.Connection, account_name: str) -> list[dict[str, Any]]:
    """Return the stable, numbered SCALE library visible to one account."""
    account_name = str(account_name or "").strip().lstrip("@")
    rows = conn.execute(
        """
        SELECT id,account_name,file_path,original_name,caption,status,content_kind
        FROM api_content_assets
        WHERE status IN ('ready','uploaded') AND COALESCE(content_kind,'scale')='scale'
          AND (account_name='' OR account_name=?)
        ORDER BY CASE WHEN status='ready' THEN 0 ELSE 1 END,
                 CASE WHEN account_name=? THEN 0 ELSE 1 END,id
        """,
        (account_name, account_name),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["asset_id"] = int(item["id"])
        item["exists"] = bool(item.get("file_path") and Path(str(item["file_path"])).is_file())
        if not item["exists"]:
            continue
        item["position"] = len(result) + 1
        result.append(item)
    return result


def _normalize_pattern_launches(launches: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in list(launches or []):
        positions: list[int] = []
        raw_positions = (raw or {}).get("positions")
        if raw_positions is None:
            raw_positions = (raw or {}).get("items") or []
        for value in list(raw_positions or []):
            if isinstance(value, dict):
                value = value.get("position") or value.get("index")
            try:
                pos = int(value or 0)
            except Exception:
                pos = 0
            if pos > 0:
                positions.append(pos)
        if not positions:
            continue
        normalized.append({
            "title": str((raw or {}).get("title") or f"Launch {len(normalized) + 1}").strip()[:120],
            "positions": positions,
        })
    if not normalized:
        raise ValueError("Add at least one launch with video numbers")
    return normalized


def preview_scale_pattern(
    conn: sqlite3.Connection,
    account_names: Iterable[str],
    launches: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    normalized = _normalize_pattern_launches(launches)
    required = sorted({pos for launch in normalized for pos in launch["positions"]})
    max_position = max(required)
    names: list[str] = []
    for raw in account_names or []:
        name = str(raw or "").strip().lstrip("@")
        if name and name not in names:
            names.append(name)
    if not names:
        raise ValueError("Select at least one account")
    rows: list[dict[str, Any]] = []
    ready = 0
    for name in names:
        library = scale_library(conn, name)
        missing = [pos for pos in required if pos > len(library)]
        ok = not missing
        ready += int(ok)
        rows.append({
            "account_name": name,
            "library_count": len(library),
            "required_max_position": max_position,
            "missing_positions": missing,
            "ready": ok,
        })
    return {
        "launches": normalized,
        "required_positions": required,
        "max_position": max_position,
        "account_count": len(rows),
        "ready_count": ready,
        "missing_count": len(rows) - ready,
        "accounts": rows,
    }


def apply_scale_pattern(
    conn: sqlite3.Connection,
    account_names: Iterable[str],
    launches: Iterable[dict[str, Any]],
    *,
    after_final: str = "repeat",
) -> dict[str, Any]:
    preview = preview_scale_pattern(conn, account_names, launches)
    missing = [row for row in preview["accounts"] if not row["ready"]]
    if missing:
        examples = ", ".join(
            f"{row['account_name']} (has {row['library_count']}, needs #{max(row['missing_positions'])})"
            for row in missing[:4]
        )
        more = f" +{len(missing) - 4} more" if len(missing) > 4 else ""
        raise ValueError(f"Some accounts do not have enough SCALE videos: {examples}{more}")
    after_final = str(after_final or "repeat").strip().lower()
    if after_final not in VALID_AFTER_FINAL:
        after_final = "repeat"
    names = [row["account_name"] for row in preview["accounts"]]
    for name in names:
        library = scale_library(conn, name)
        plan_sets = []
        for launch_no, launch in enumerate(preview["launches"], start=1):
            plan_sets.append({
                "title": str(launch.get("title") or f"Launch {launch_no}"),
                "items": [{"asset_id": int(library[pos - 1]["asset_id"])} for pos in launch["positions"]],
            })
        save_plan(conn, name, plan_sets, current_set_order=0, after_final=after_final)
    return {
        "accounts": names,
        "launches": preview["launches"],
        "after_final": after_final,
        "ready_count": len(names),
    }


def complete_plan_item(conn: sqlite3.Connection, account_name: str, plan_item_id: int, *, commit: bool = True) -> dict[str, Any]:
    """Persist progress after one successful custom-plan publication.

    This prevents a failed third item from causing the first two items to be
    uploaded again on the next run.
    """
    account_name = str(account_name or "").strip().lstrip("@")
    try:
        plan_item_id = int(plan_item_id or 0)
    except Exception:
        plan_item_id = 0
    plan = get_plan(conn, account_name)
    if plan["strategy"] != "custom" or not plan["sets"] or not plan_item_id:
        return {"updated": False, "strategy": plan["strategy"], "reason": "not_custom_item"}
    set_order = max(0, min(int(plan["current_set_order"]), len(plan["sets"]) - 1))
    current_set = plan["sets"][set_order]
    item_index = next((idx for idx, item in enumerate(current_set["items"]) if int(item.get("id") or 0) == plan_item_id), -1)
    if item_index < 0:
        return {"updated": False, "strategy": "custom", "reason": "item_not_in_current_launch"}
    already = int(plan.get("current_item_order") or 0)
    if item_index < already:
        return {
            "updated": False,
            "strategy": "custom",
            "reason": "already_completed",
            "current_set_order": set_order,
            "current_item_order": already,
        }
    next_item_order = item_index + 1
    launch_completed = next_item_order >= len(current_set["items"])
    stopped = 0
    next_set_order = set_order
    if launch_completed:
        next_item_order = 0
        if set_order + 1 < len(plan["sets"]):
            next_set_order = set_order + 1
        elif plan["after_final"] == "stop":
            stopped = 1
        else:
            next_set_order = 0
    conn.execute(
        """
        UPDATE ig_account_content_plan_state
        SET current_set_order=?,current_item_order=?,is_stopped=?,updated_at=datetime('now')
        WHERE account_name=?
        """,
        (next_set_order, next_item_order, stopped, account_name),
    )
    if commit:
        conn.commit()
    return {
        "updated": True,
        "strategy": "custom",
        "launch_completed": launch_completed,
        "previous_set_order": set_order,
        "current_set_order": next_set_order,
        "current_item_order": next_item_order,
        "is_stopped": bool(stopped),
        "after_final": plan["after_final"],
    }

def plan_summaries(conn: sqlite3.Connection, account_names: Iterable[str], preview_limit: int = 4) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_name in account_names:
        name = str(raw_name or "")
        if not name:
            continue
        plan = get_plan(conn, name)
        if plan["strategy"] == "standard":
            asset = plan.get("standard_asset")
            previews = []
            if asset:
                previews = [{
                    "asset_id": int(asset.get("asset_id") or 0),
                    "original_name": str(asset.get("original_name") or ""),
                    "file_path": str(asset.get("file_path") or ""),
                    "caption": str(asset.get("caption") or ""),
                }]
            result[name] = {
                "configured": bool(asset),
                "strategy": "standard",
                "is_custom": False,
                "posts_per_run": int(plan["posts_per_run"]),
                "standard_asset_id": int(plan["standard_asset_id"] or 0),
                "set_count": 0,
                "item_count": 1 if asset else 0,
                "current_set_order": 0,
                "current_set_title": "Standard",
                "current_set_posts": int(plan["posts_per_run"]) if asset else 0,
                "next_assets": previews[: max(1, int(preview_limit))],
                "next_assets_total": 1 if asset else 0,
                "after_final": "repeat",
                "is_stopped": False,
            }
            continue

        sets = plan["sets"]
        current = int(plan["current_set_order"] or 0)
        if sets:
            current = max(0, min(current, len(sets) - 1))
            selected = sets[current]
            item_order = max(0, min(int(plan.get("current_item_order") or 0), len(selected["items"])))
            remaining_items = selected["items"][item_order:]
            previews = remaining_items[: max(1, int(preview_limit))]
            result[name] = {
                "configured": True,
                "strategy": "custom",
                "is_custom": True,
                "posts_per_run": len(remaining_items),
                "standard_asset_id": int(plan["standard_asset_id"] or 0),
                "set_count": len(sets),
                "item_count": sum(len(s["items"]) for s in sets),
                "current_set_order": current,
                "current_set_title": selected["title"] or f"Launch {current + 1}",
                "current_set_posts": len(remaining_items),
                "current_item_order": item_order,
                "completed_items": item_order,
                "set_items_total": len(selected["items"]),
                "next_assets": previews,
                "next_assets_total": len(remaining_items),
                "after_final": plan["after_final"],
                "is_stopped": bool(plan["is_stopped"]),
            }
        else:
            result[name] = {
                "configured": False,
                "strategy": "custom",
                "is_custom": True,
                "posts_per_run": 0,
                "standard_asset_id": int(plan["standard_asset_id"] or 0),
                "set_count": 0,
                "item_count": 0,
                "current_set_order": 0,
                "current_set_title": "Content pattern",
                "current_set_posts": 0,
                "next_assets": [],
                "next_assets_total": 0,
                "after_final": plan["after_final"],
                "is_stopped": False,
            }
    return result
