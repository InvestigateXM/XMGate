"""End-to-end flow against a fake Bot: join request, Mini App API, outcomes."""

import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xmgate import captcha
from xmgate.api import setup_api
from xmgate.config import Config
from xmgate.db import DB
from xmgate.flow import Gate
from xmgate.initdata import sign_init_data

TOKEN = "123456:TEST"
USER = 1001
CHAT = -100500


class FakeBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, kwargs))
            return SimpleNamespace(message_id=77)

        return call

    def names(self):
        return [c[0] for c in self.calls]


def join_request(query_id="q-1", user_id=USER):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="agent"),
        chat=SimpleNamespace(id=CHAT, title="Test Ingress Chat", username="testingress"),
        user_chat_id=user_id,
        query_id=query_id,
    )


@pytest.fixture
async def env():
    cfg = Config(TOKEN, "https://gate.example", "s", "webhook", 300, ":memory:", 0)
    db = await DB.open(":memory:")
    bot = FakeBot()
    gate = Gate(bot, db, cfg)
    app = web.Application()
    setup_api(app, gate)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield SimpleNamespace(gate=gate, bot=bot, db=db, client=client)
    await client.close()
    await db.close()


async def call(env, path, jr_id, user_id=USER, **body):
    init = sign_init_data({"user": {"id": user_id, "first_name": "A"}, "auth_date": int(time.time())}, TOKEN)
    token = env.gate.request_token(jr_id, user_id)
    res = await env.client.post(f"/api/{path}", json={"r": token, **body}, headers={"Authorization": f"tma {init}"})
    return res.status, (await res.json() if res.status == 200 else await res.text())


def answer_ids(stored):
    return [t["id"] for t in stored if t["answer"]]


async def latest_attempt(env):
    row = await env.db._one("SELECT * FROM attempts ORDER BY issued_at DESC LIMIT 1")
    return row["id"], json.loads(row["tiles"])


async def test_unknown_user_gets_mini_app_then_approved(env):
    await env.gate.on_join_request(join_request())
    assert env.bot.names() == ["send_chat_join_request_web_app"]
    url = env.bot.calls[0][1]["web_app_url"]
    assert url.startswith("https://gate.example/app/?r=1.")

    status, data = await call(env, "challenge", 1)
    assert status == 200 and data["status"] == "challenge"
    assert len(data["tiles"]) == captcha.TILE_COUNT
    assert "answer" not in json.dumps(data["tiles"])

    attempt, stored = await latest_attempt(env)
    status, data = await call(env, "answer", 1, attempt=attempt, picked=answer_ids(stored))
    assert data["status"] == "approved"
    assert env.bot.calls[-1] == ("answer_chat_join_request_query", {"chat_join_request_query_id": "q-1", "result": "approve"})
    assert (await env.db.get_user(USER))["verified_method"] == "captcha"


async def test_verified_user_is_approved_instantly(env):
    await env.db.mark_verified(USER, "captcha", -1)
    await env.gate.on_join_request(join_request())
    assert env.bot.calls == [("answer_chat_join_request_query", {"chat_join_request_query_id": "q-1", "result": "approve"})]


async def test_wrong_answer_restarts_and_throttles(env):
    await env.gate.on_join_request(join_request())
    for _ in range(3):
        await call(env, "challenge", 1)
        attempt, _ = await latest_attempt(env)
        _, data = await call(env, "answer", 1, attempt=attempt, picked=["x", "y"])
        assert data["status"] == "wrong"
    _, data = await call(env, "challenge", 1)
    assert data["status"] == "wait" and 0 < data["wait"] <= 5
    # never declined for being wrong
    assert "answer_chat_join_request_query" not in env.bot.names()


async def test_attempt_cannot_be_reused(env):
    await env.gate.on_join_request(join_request())
    await call(env, "challenge", 1)
    attempt, stored = await latest_attempt(env)
    await call(env, "answer", 1, attempt=attempt, picked=["x", "y"])
    status, _ = await call(env, "answer", 1, attempt=attempt, picked=answer_ids(stored))
    assert status == 409


async def test_manual_button_queues(env):
    await env.gate.on_join_request(join_request())
    _, data = await call(env, "manual", 1)
    assert data["status"] == "queued"
    assert env.bot.calls[-1][1]["result"] == "queue"
    await env.gate.on_member_joined(USER, CHAT)
    assert (await env.db.get_user(USER))["verified_method"] == "manual"


async def test_minimised_app_declined_after_grace_and_can_still_verify(env):
    await env.gate.on_join_request(join_request())
    await call(env, "event", 1, type="deactivated")
    await env.db.update_join_request(1, grace_until=time.time() - 1)
    await env.gate.sweep()
    assert env.bot.calls[-1][1]["result"] == "decline"

    _, data = await call(env, "challenge", 1)
    assert data["status"] == "challenge" and data["request_open"] is False
    attempt, stored = await latest_attempt(env)
    _, data = await call(env, "answer", 1, attempt=attempt, picked=answer_ids(stored))
    assert data["status"] == "verified_late"
    assert (await env.db.get_user(USER))["verified_at"]


async def test_activated_clears_grace(env):
    await env.gate.on_join_request(join_request())
    await call(env, "event", 1, type="deactivated")
    assert (await env.db.get_join_request(1))["grace_until"]
    await call(env, "event", 1, type="activated")
    assert (await env.db.get_join_request(1))["grace_until"] is None


async def test_lost_heartbeat_declines(env):
    await env.gate.on_join_request(join_request())
    await env.db.update_join_request(1, last_heartbeat=time.time() - 301)
    await env.gate.sweep()
    assert env.bot.calls[-1][1]["result"] == "decline"


async def test_dm_fallback_without_query_id(env):
    await env.gate.on_join_request(join_request(query_id=None))
    assert env.bot.names() == ["send_message"]
    await call(env, "challenge", 1)
    attempt, stored = await latest_attempt(env)
    _, data = await call(env, "answer", 1, attempt=attempt, picked=answer_ids(stored))
    assert data["status"] == "approved"
    assert "approve_chat_join_request" in env.bot.names()


async def test_link_is_bound_to_the_user(env):
    await env.gate.on_join_request(join_request())
    init = sign_init_data({"user": {"id": 999}, "auth_date": int(time.time())}, TOKEN)
    token = env.gate.request_token(1, USER)
    res = await env.client.post("/api/challenge", json={"r": token}, headers={"Authorization": f"tma {init}"})
    assert res.status == 403


async def test_forged_init_data_rejected(env):
    await env.gate.on_join_request(join_request())
    init = sign_init_data({"user": {"id": USER}, "auth_date": int(time.time())}, "other:token")
    res = await env.client.post(
        "/api/challenge", json={"r": env.gate.request_token(1, USER)}, headers={"Authorization": f"tma {init}"}
    )
    assert res.status == 401
