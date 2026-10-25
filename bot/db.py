"""Хранилище данных на SQLite (aiosqlite)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

import aiosqlite

from .utils import now_iso, now_utc, to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT NOT NULL,
    language_code TEXT,
    is_blocked    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    category     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    preview      TEXT,
    payload      TEXT NOT NULL,
    anonymous    INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'new',
    created_at   TEXT NOT NULL,
    processed_at TEXT,
    processed_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_reports_user_created
    ON reports (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_status
    ON reports (status, id DESC);

CREATE TABLE IF NOT EXISTS broadcasts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    payload          TEXT NOT NULL,
    preview          TEXT,
    kind             TEXT NOT NULL,
    interval_seconds INTEGER,
    next_run_at      TEXT,
    last_run_at      TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_by       INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_broadcasts_due
    ON broadcasts (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS broadcast_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    sent         INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (broadcast_id) REFERENCES broadcasts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_broadcast
    ON broadcast_runs (broadcast_id, id DESC);
"""


class Database:
    """Тонкая обёртка над aiosqlite с методами предметной области."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    # --- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена")
        return self._conn

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = await self._fetchone(sql, params)
        return row[0] if row is not None else None

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.lastrowid or 0

    # --- пользователи -----------------------------------------------------

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        language_code: str | None = None,
    ) -> None:
        moment = now_iso()
        await self._execute(
            """
            INSERT INTO users (user_id, username, full_name, language_code, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                language_code = excluded.language_code,
                last_seen_at = excluded.last_seen_at
            """,
            (user_id, username, full_name, language_code, moment, moment),
        )

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))

    async def set_blocked(self, user_id: int, blocked: bool = True) -> None:
        await self._execute(
            "UPDATE users SET is_blocked = ? WHERE user_id = ?", (int(blocked), user_id)
        )

    async def is_blocked(self, user_id: int) -> bool:
        value = await self._scalar("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        return bool(value)

    async def count_users(self, only_active: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM users"
        if only_active:
            sql += " WHERE is_blocked = 0"
        return int(await self._scalar(sql) or 0)

    async def count_new_users_since(self, iso_moment: str) -> int:
        return int(
            await self._scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (iso_moment,))
            or 0
        )

    async def broadcast_recipients(self) -> list[int]:
        rows = await self._fetchall("SELECT user_id FROM users WHERE is_blocked = 0")
        return [row["user_id"] for row in rows]

    # --- обращения --------------------------------------------------------

    async def create_report(
        self,
        *,
        user_id: int,
        category: str,
        kind: str,
        preview: str | None,
        payload: str,
        anonymous: bool,
    ) -> int:
        return await self._execute(
            """
            INSERT INTO reports (user_id, category, kind, preview, payload, anonymous, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (user_id, category, kind, preview, payload, int(anonymous), now_iso()),
        )

    async def get_report(self, report_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))

    async def last_report_at(self, user_id: int) -> str | None:
        return await self._scalar(
            "SELECT MAX(created_at) FROM reports WHERE user_id = ?", (user_id,)
        )

    async def count_reports(self, status: str | None = None, since: str | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(await self._scalar(f"SELECT COUNT(*) FROM reports{where}", params) or 0)

    async def list_reports(
        self, *, status: str | None = None, limit: int = 5, offset: int = 0
    ) -> list[aiosqlite.Row]:
        params: list[Any] = []
        where = ""
        if status is not None:
            where = " WHERE status = ?"
            params.append(status)
        params.extend([limit, offset])
        return await self._fetchall(
            f"SELECT * FROM reports{where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        )

    async def set_report_status(
        self, report_id: int, status: str, admin_id: int | None = None
    ) -> None:
        processed_at = now_iso() if status in {"done", "spam"} else None
        await self._execute(
            "UPDATE reports SET status = ?, processed_at = ?, processed_by = ? WHERE id = ?",
            (status, processed_at, admin_id, report_id),
        )

    async def all_reports(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM reports ORDER BY id ASC")

    async def all_reports_with_users(self) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT r.*, u.full_name AS user_full_name, u.username AS user_username
            FROM reports AS r
            LEFT JOIN users AS u ON u.user_id = r.user_id
            ORDER BY r.id ASC
            """
        )


    # --- рассылки ---------------------------------------------------------

    async def create_broadcast(
        self,
        *,
        payload: str,
        preview: str | None,
        kind: str,
        interval_seconds: int | None,
        next_run_at: str | None,
        enabled: bool,
        created_by: int,
    ) -> int:
        return await self._execute(
            """
            INSERT INTO broadcasts
                (payload, preview, kind, interval_seconds, next_run_at, enabled, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload,
                preview,
                kind,
                interval_seconds,
                next_run_at,
                int(enabled),
                created_by,
                now_iso(),
            ),
        )

    async def get_broadcast(self, broadcast_id: int) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,))

    async def list_broadcasts(self) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM broadcasts ORDER BY id DESC")

    async def count_broadcasts(self, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM broadcasts"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return int(await self._scalar(sql) or 0)

    async def set_broadcast_enabled(self, broadcast_id: int, enabled: bool) -> None:
        if enabled:
            await self._execute(
                """
                UPDATE broadcasts
                SET enabled = 1, next_run_at = COALESCE(next_run_at, ?)
                WHERE id = ?
                """,
                (now_iso(), broadcast_id),
            )
        else:
            await self._execute("UPDATE broadcasts SET enabled = 0 WHERE id = ?", (broadcast_id,))

    async def delete_broadcast(self, broadcast_id: int) -> None:
        await self._execute("DELETE FROM broadcasts WHERE id = ?", (broadcast_id,))

    async def due_broadcasts(self, moment: str | None = None) -> list[aiosqlite.Row]:
        return await self._fetchall(
            """
            SELECT * FROM broadcasts
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC
            """,
            (moment or now_iso(),),
        )

    async def mark_broadcast_ran(
        self,
        broadcast_id: int,
        *,
        last_run_at: str,
        next_run_at: str | None,
        enabled: bool,
    ) -> None:
        await self._execute(
            """
            UPDATE broadcasts
            SET last_run_at = ?, next_run_at = ?, enabled = ?
            WHERE id = ?
            """,
            (last_run_at, next_run_at, int(enabled), broadcast_id),
        )

    # --- журнал прогонов --------------------------------------------------

    async def start_broadcast_run(self, broadcast_id: int) -> int:
        return await self._execute(
            "INSERT INTO broadcast_runs (broadcast_id, started_at) VALUES (?, ?)",
            (broadcast_id, now_iso()),
        )

    async def finish_broadcast_run(self, run_id: int, sent: int, failed: int) -> None:
        await self._execute(
            "UPDATE broadcast_runs SET finished_at = ?, sent = ?, failed = ? WHERE id = ?",
            (now_iso(), sent, failed, run_id),
        )

    async def last_broadcast_run(self, broadcast_id: int) -> aiosqlite.Row | None:
        return await self._fetchone(
            "SELECT * FROM broadcast_runs WHERE broadcast_id = ? ORDER BY id DESC LIMIT 1",
            (broadcast_id,),
        )

    async def broadcast_totals(self, broadcast_id: int) -> tuple[int, int]:
        row = await self._fetchone(
            """
            SELECT COALESCE(SUM(sent), 0) AS sent, COALESCE(SUM(failed), 0) AS failed
            FROM broadcast_runs WHERE broadcast_id = ?
            """,
            (broadcast_id,),
        )
        return (int(row["sent"]), int(row["failed"])) if row else (0, 0)

    # --- сводка -----------------------------------------------------------

    async def stats(self) -> dict[str, int]:
        day_ago = to_iso(now_utc() - timedelta(hours=24))
        return {
            "users_total": await self.count_users(),
            "users_active": await self.count_users(only_active=True),
            "users_day": await self.count_new_users_since(day_ago),
            "reports_total": await self.count_reports(),
            "reports_new": await self.count_reports(status="new"),
            "reports_day": await self.count_reports(since=day_ago),
            "broadcasts_total": await self.count_broadcasts(),
            "broadcasts_enabled": await self.count_broadcasts(enabled_only=True),
        }

