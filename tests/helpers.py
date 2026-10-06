"""Shared by the PostgreSQL tests: a fake Bot, join requests, and Mini App API calls."""

import os
import time
from types import SimpleNamespace

import pytest

from xmgate.db import DB
from xmgate.initdata import sign_init_data

TOKEN = "123456:TEST"
USER = 1001
CHAT = -100500

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_postgres = pytest.mark.skipif(not DATABASE_URL, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")


class FakeBot:
    def __init__(self):
        self.calls = []
        self.replies = {}  # method name -> return value, or a function of the call's kwargs

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            reply = self.replies.get(name)
            if callable(reply):
                return reply(**kwargs)
            return reply if reply is not None else SimpleNamespace(message_id=77)

        return call

    def names(self):
        return [c[0] for c in self.calls]


def join_request(query_id="q-1", user_id=USER):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="agent"),
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="Test Ingress Chat", username="testingress"),
        user_chat_id=user_id,
        query_id=query_id,
    )


async def fresh_db() -> DB:
    import asyncpg

    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await conn.close()
    return await DB.open(DATABASE_URL)


async def call(env, path, jr_id, user_id=USER, **body):
    init = sign_init_data({"user": {"id": user_id, "first_name": "A"}, "auth_date": int(time.time())}, TOKEN)
    token = env.gate.self_token(user_id) if jr_id == "self" else env.gate.request_token(jr_id, user_id)
    res = await env.client.post(f"/api/{path}", json={"r": token, **body}, headers={"Authorization": f"tma {init}"})
    return res.status, (await res.json() if res.status == 200 else await res.text())


def answer_ids(stored):
    return [t["id"] for t in stored if t["answer"]]


async def latest_attempt(env):
    row = await env.db._one("SELECT * FROM attempts ORDER BY issued_at DESC LIMIT 1")
    return row["id"], row["tiles"]
