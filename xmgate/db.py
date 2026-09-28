"""SQLite storage for the prototype. The full design uses PostgreSQL (design §10)."""

import json
import os
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  tg_user_id      INTEGER PRIMARY KEY,
  verified_at     REAL,            -- NULL = unknown
  verified_method TEXT,            -- captcha | manual
  verified_chat   INTEGER,
  first_seen_at   REAL NOT NULL,
  last_username   TEXT
);

CREATE TABLE IF NOT EXISTS join_requests (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id         INTEGER NOT NULL,
  chat_title      TEXT,
  chat_username   TEXT,
  tg_user_id      INTEGER NOT NULL,
  user_chat_id    INTEGER NOT NULL,
  path            TEXT NOT NULL,   -- query | dm
  query_id        TEXT,
  outcome         TEXT NOT NULL DEFAULT 'pending',
                  -- pending | captcha | approved | queued | declined_timeout | admin_approved
  requested_at    REAL NOT NULL,
  resolved_at     REAL,
  dm_message_id   INTEGER,
  last_heartbeat  REAL,
  grace_until     REAL
);
CREATE INDEX IF NOT EXISTS jr_user ON join_requests (tg_user_id, outcome);
CREATE INDEX IF NOT EXISTS jr_open ON join_requests (outcome);

CREATE TABLE IF NOT EXISTS attempts (
  id              TEXT PRIMARY KEY,
  tg_user_id      INTEGER NOT NULL,
  join_request_id INTEGER,
  tiles           TEXT NOT NULL,   -- JSON [{id, key, answer}]
  issued_at       REAL NOT NULL,
  answered_at     REAL,
  correct         INTEGER,
  solve_ms        INTEGER,
  had_touch       INTEGER
);
CREATE INDEX IF NOT EXISTS attempts_user ON attempts (tg_user_id, issued_at);
"""

OPEN = ("pending", "captcha")


class DB:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "DB":
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def _one(self, sql: str, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    # users

    async def upsert_user(self, user_id: int, username: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO users (tg_user_id, first_seen_at, last_username) VALUES (?, ?, ?) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET last_username = excluded.last_username",
            (user_id, time.time(), username),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE tg_user_id = ?", user_id)

    async def mark_verified(self, user_id: int, method: str, chat_id: int | None) -> None:
        await self.conn.execute(
            "INSERT INTO users (tg_user_id, first_seen_at, verified_at, verified_method, verified_chat) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (tg_user_id) DO UPDATE SET "
            "verified_at = excluded.verified_at, verified_method = excluded.verified_method, "
            "verified_chat = excluded.verified_chat",
            (user_id, time.time(), time.time(), method, chat_id),
        )
        await self.conn.commit()

    async def forget_user(self, user_id: int) -> None:
        for table in ("attempts", "join_requests", "users"):
            await self.conn.execute(f"DELETE FROM {table} WHERE tg_user_id = ?", (user_id,))
        await self.conn.commit()

    # join requests

    async def add_join_request(self, **row) -> int:
        row.setdefault("requested_at", time.time())
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = await self.conn.execute(f"INSERT INTO join_requests ({cols}) VALUES ({marks})", tuple(row.values()))
        await self.conn.commit()
        return cur.lastrowid

    async def get_join_request(self, jr_id: int):
        return await self._one("SELECT * FROM join_requests WHERE id = ?", jr_id)

    async def update_join_request(self, jr_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(f"UPDATE join_requests SET {sets} WHERE id = ?", (*fields.values(), jr_id))
        await self.conn.commit()

    async def claim_join_request(self, jr_id: int, outcome: str, from_outcomes=OPEN) -> bool:
        """Moves a request to a final outcome once. False if something else already resolved it."""
        marks = ", ".join("?" for _ in from_outcomes)
        cur = await self.conn.execute(
            f"UPDATE join_requests SET outcome = ?, resolved_at = ? WHERE id = ? AND outcome IN ({marks})",
            (outcome, time.time(), jr_id, *from_outcomes),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def open_requests_for_user(self, user_id: int):
        return await self._all(
            "SELECT * FROM join_requests WHERE tg_user_id = ? AND outcome IN ('pending', 'captcha')", user_id
        )

    async def queued_request(self, user_id: int, chat_id: int):
        return await self._one(
            "SELECT * FROM join_requests WHERE tg_user_id = ? AND chat_id = ? AND outcome = 'queued' "
            "ORDER BY id DESC LIMIT 1",
            user_id,
            chat_id,
        )

    async def expired_requests(self, now: float, grace_seconds: int):
        return await self._all(
            "SELECT * FROM join_requests WHERE outcome IN ('pending', 'captcha') AND ("
            "  (grace_until IS NOT NULL AND grace_until < ?)"
            "  OR (path = 'query' AND last_heartbeat IS NOT NULL AND last_heartbeat < ?))",
            now,
            now - grace_seconds,
        )

    # attempts

    async def add_attempt(self, attempt_id: str, user_id: int, jr_id: int | None, tiles: list) -> None:
        await self.conn.execute(
            "INSERT INTO attempts (id, tg_user_id, join_request_id, tiles, issued_at) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, user_id, jr_id, json.dumps(tiles), time.time()),
        )
        await self.conn.commit()

    async def get_attempt(self, attempt_id: str):
        return await self._one("SELECT * FROM attempts WHERE id = ?", attempt_id)

    async def answer_attempt(self, attempt_id: str, correct: bool, solve_ms: int | None, had_touch: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE attempts SET answered_at = ?, correct = ?, solve_ms = ?, had_touch = ? "
            "WHERE id = ? AND answered_at IS NULL",
            (time.time(), int(correct), solve_ms, int(had_touch), attempt_id),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def recent_misses(self, user_id: int, since: float):
        """Wrong answers since `since`, newest first, stopping at the last correct one."""
        rows = await self._all(
            "SELECT correct, answered_at FROM attempts WHERE tg_user_id = ? AND answered_at IS NOT NULL "
            "AND answered_at > ? ORDER BY answered_at DESC",
            user_id,
            since,
        )
        misses = []
        for row in rows:
            if row["correct"]:
                break
            misses.append(row["answered_at"])
        return misses
