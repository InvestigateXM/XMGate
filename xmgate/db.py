"""PostgreSQL storage (design §10).

Times are timestamptz in the database and plain epoch seconds (float) in Python;
a type codec converts between the two, so the rest of the code never handles
datetime objects.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = (Path(__file__).resolve().parent / "schema.sql").read_text()
OPEN = ("pending", "captcha")

_EPOCH_2000 = 946684800  # PostgreSQL stores timestamps as microseconds since 2000-01-01


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "timestamptz",
        schema="pg_catalog",
        format="tuple",
        encoder=lambda t: (round((t - _EPOCH_2000) * 1_000_000),),
        decoder=lambda v: v[0] / 1_000_000 + _EPOCH_2000,
    )
    await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)


def _count(status: str) -> int:
    """Rows touched, from a status string such as 'UPDATE 1'."""
    return int(status.rsplit(" ", 1)[-1])


class DB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def open(cls, dsn: str, attempts: int = 30) -> "DB":
        for attempt in range(1, attempts + 1):
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, init=_init_connection)
                break
            except (OSError, asyncpg.CannotConnectNowError) as e:
                if attempt == attempts:
                    raise
                log.info("Waiting for PostgreSQL (%s)", e)
                await asyncio.sleep(1)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, *args):
        return await self.pool.fetchrow(sql, *args)

    async def _all(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)

    async def _run(self, sql: str, *args) -> int:
        return _count(await self.pool.execute(sql, *args))

    # users

    async def upsert_user(self, user_id: int, username: str | None) -> None:
        await self._run(
            "INSERT INTO users (tg_user_id, last_username) VALUES ($1, $2) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET last_username = coalesce(excluded.last_username, users.last_username)",
            user_id,
            username,
        )

    async def get_user(self, user_id: int):
        return await self._one("SELECT * FROM users WHERE tg_user_id = $1", user_id)

    async def mark_verified(self, user_id: int, method: str, chat_id: int | None) -> None:
        await self._run(
            "INSERT INTO users (tg_user_id, verified_at, verified_method, verified_chat) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET verified_at = excluded.verified_at, "
            "verified_method = excluded.verified_method, verified_chat = excluded.verified_chat",
            user_id,
            time.time(),
            method,
            chat_id,
        )

    async def forget_user(self, user_id: int) -> bool:
        """Deletes every row about the user; join requests and attempts go with it."""
        return await self._run("DELETE FROM users WHERE tg_user_id = $1", user_id) == 1

    async def user_summary(self, user_id: int):
        return await self._one(
            "SELECT u.*, "
            " (SELECT count(*) FROM join_requests j WHERE j.tg_user_id = u.tg_user_id) AS requests,"
            " (SELECT count(*) FROM join_requests j WHERE j.tg_user_id = u.tg_user_id"
            "   AND j.outcome IN ('pending', 'captcha', 'queued')) AS open_requests,"
            " (SELECT count(*) FROM attempts a WHERE a.tg_user_id = u.tg_user_id) AS attempts,"
            " (SELECT chat_title FROM join_requests j WHERE j.tg_user_id = u.tg_user_id"
            "   AND j.chat_id = u.verified_chat ORDER BY j.id DESC LIMIT 1) AS verified_chat_title "
            "FROM users u WHERE u.tg_user_id = $1",
            user_id,
        )

    # opt-out markers (design §9): only a keyed hash of the user id is stored

    async def is_opted_out(self, marker: bytes) -> bool:
        return await self.pool.fetchval("SELECT true FROM opt_outs WHERE marker = $1", marker) is not None

    async def add_opt_out(self, marker: bytes) -> None:
        await self._run("INSERT INTO opt_outs (marker) VALUES ($1) ON CONFLICT DO NOTHING", marker)

    async def remove_opt_out(self, marker: bytes) -> bool:
        return await self._run("DELETE FROM opt_outs WHERE marker = $1", marker) == 1

    # join requests

    async def add_join_request(self, **row) -> int:
        cols = ", ".join(row)
        marks = ", ".join(f"${i}" for i in range(1, len(row) + 1))
        return await self.pool.fetchval(
            f"INSERT INTO join_requests ({cols}) VALUES ({marks}) RETURNING id", *row.values()
        )

    async def get_join_request(self, jr_id: int):
        return await self._one("SELECT * FROM join_requests WHERE id = $1", jr_id)

    async def update_join_request(self, jr_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=2))
        await self._run(f"UPDATE join_requests SET {sets} WHERE id = $1", jr_id, *fields.values())

    async def claim_join_request(self, jr_id: int, outcome: str, from_outcomes=OPEN) -> bool:
        """Moves a request to a final outcome once. False if something else already resolved it."""
        return (
            await self._run(
                "UPDATE join_requests SET outcome = $2, resolved_at = now() WHERE id = $1 AND outcome = ANY($3)",
                jr_id,
                outcome,
                list(from_outcomes),
            )
            == 1
        )

    async def open_requests_for_user(self, user_id: int):
        return await self._all(
            "SELECT * FROM join_requests WHERE tg_user_id = $1 AND outcome IN ('pending', 'captcha')", user_id
        )

    async def queued_request(self, user_id: int, chat_id: int):
        return await self._one(
            "SELECT * FROM join_requests WHERE tg_user_id = $1 AND chat_id = $2 AND outcome = 'queued' "
            "ORDER BY id DESC LIMIT 1",
            user_id,
            chat_id,
        )

    async def expired_requests(self, now: float, grace_seconds: int):
        return await self._all(
            "SELECT * FROM join_requests WHERE outcome IN ('pending', 'captcha') AND ("
            "  (grace_until IS NOT NULL AND grace_until < $1)"
            "  OR (path = 'query' AND last_heartbeat IS NOT NULL AND last_heartbeat < $2))",
            now,
            now - grace_seconds,
        )

    # attempts

    async def add_attempt(self, attempt_id: str, user_id: int, jr_id: int | None, tiles: list) -> None:
        await self._run(
            "INSERT INTO attempts (id, tg_user_id, join_request_id, tiles) VALUES ($1, $2, $3, $4)",
            attempt_id,
            user_id,
            jr_id,
            tiles,
        )

    async def get_attempt(self, attempt_id: str):
        return await self._one("SELECT * FROM attempts WHERE id = $1", attempt_id)

    async def answer_attempt(self, attempt_id: str, correct: bool, solve_ms: int | None, had_touch: bool) -> bool:
        return (
            await self._run(
                "UPDATE attempts SET answered_at = now(), correct = $2, solve_ms = $3, had_touch = $4 "
                "WHERE id = $1 AND answered_at IS NULL",
                attempt_id,
                correct,
                solve_ms,
                had_touch,
            )
            == 1
        )

    async def recent_misses(self, user_id: int, since: float):
        """Wrong answers since `since`, newest first, stopping at the last correct one."""
        rows = await self._all(
            "SELECT correct, answered_at FROM attempts WHERE tg_user_id = $1 AND answered_at IS NOT NULL "
            "AND answered_at > $2 ORDER BY answered_at DESC",
            user_id,
            since,
        )
        misses = []
        for row in rows:
            if row["correct"]:
                break
            misses.append(row["answered_at"])
        return misses
