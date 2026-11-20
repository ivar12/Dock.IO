"""Tiny SQLite store for users, update schedules and the audit log."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "dockermgr.db"


@contextmanager
def connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT,
                is_local INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                cron TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('all', 'selected')),
                containers TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            """
        )


# --------------------------------------------------------------------------- users
def upsert_local_user(username: str, password_hash: str) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO users (username, password_hash, is_local)
               VALUES (?, ?, 1)
               ON CONFLICT(username) DO UPDATE SET
                   password_hash = excluded.password_hash,
                   is_local = 1""",
            (username, password_hash),
        )


def get_user(username: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def ensure_oidc_user(username: str) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO users (username, password_hash, is_local)
               VALUES (?, NULL, 0)
               ON CONFLICT(username) DO NOTHING""",
            (username,),
        )


# ----------------------------------------------------------------------- schedules
def list_schedules() -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    return [_schedule_dict(r) for r in rows]


def get_schedule(schedule_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
    return _schedule_dict(row) if row else None


def create_schedule(name: str, cron: str, mode: str, containers: list[str], enabled: bool) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO schedules (name, cron, mode, containers, enabled) VALUES (?, ?, ?, ?, ?)",
            (name, cron, mode, json.dumps(containers), 1 if enabled else 0),
        )
        return int(cur.lastrowid)


def update_schedule(schedule_id: int, **fields) -> None:
    allowed = {"name", "cron", "mode", "containers", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "containers" in updates:
        updates["containers"] = json.dumps(updates["containers"])
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with connection() as conn:
        conn.execute(
            f"UPDATE schedules SET {assignments} WHERE id = ?",
            (*updates.values(), schedule_id),
        )


def delete_schedule(schedule_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def _schedule_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "cron": row["cron"],
        "mode": row["mode"],
        "containers": json.loads(row["containers"] or "[]"),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------- audit
def log_action(actor: str, action: str, detail: str = "") -> None:
    with connection() as conn:
        conn.execute(
            "INSERT INTO audit (actor, action, detail) VALUES (?, ?, ?)",
            (actor, action, detail[:4000]),
        )


def list_audit(limit: int = 100) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT ts, actor, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
