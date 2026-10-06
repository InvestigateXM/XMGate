"""End-to-end flow against a fake Bot and a real PostgreSQL: join request, Mini App API, DM menu actions.

Needs TEST_DATABASE_URL, a database the tests may wipe, for example:
    docker run -d --name xmgate-test-db -e POSTGRES_PASSWORD=test -p 5433:5432 postgres:16-alpine
    export TEST_DATABASE_URL=postgresql://postgres:test@localhost:5433/postgres
"""

import time

from helpers import CHAT, TOKEN, USER, answer_ids, call, join_request, latest_attempt, needs_postgres
from xmgate import captcha
from xmgate.initdata import sign_init_data

pytestmark = needs_postgres


async def test_unknown_user_gets_mini_app_then_approved(env):
    await env.gate.on_join_request(join_request())
    assert env.bot.names() == ["send_chat_join_request_web_app"]
    url = env.bot.calls[0][1]["web_app_url"]
    assert url.startswith("https://gate.example/app/?r=1.")

    status, data = await call(env, "challenge", 1)
    assert status == 200 and data["status"] == "challenge"
    assert len(data["tiles"]) == captcha.TILE_COUNT
    assert all(set(t) == {"id", "img"} for t in data["tiles"])

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


async def test_self_verification_from_dm_menu_approves_pending_requests(env):
    await env.gate.on_join_request(join_request(query_id=None))
    _, data = await call(env, "challenge", "self")
    assert data["status"] == "challenge" and data["self"] is True
    attempt, stored = await latest_attempt(env)
    _, data = await call(env, "answer", "self", attempt=attempt, picked=answer_ids(stored))
    assert data["status"] == "verified_self" and data["approved"] == 1
    assert "approve_chat_join_request" in env.bot.names()
    _, data = await call(env, "challenge", "self")
    assert data["status"] == "already_verified"


async def test_self_mode_has_no_admin_button(env):
    status, _ = await call(env, "manual", "self")
    assert status == 400


async def test_self_token_is_bound_to_the_user(env):
    init = sign_init_data({"user": {"id": 999}, "auth_date": int(time.time())}, TOKEN)
    res = await env.client.post(
        "/api/challenge", json={"r": env.gate.self_token(USER)}, headers={"Authorization": f"tma {init}"}
    )
    assert res.status == 403


async def test_delete_my_data_removes_everything_and_queues_open_requests(env):
    await env.gate.on_join_request(join_request())
    await call(env, "challenge", 1)
    await env.gate.forget(USER, ignore=False)
    assert env.bot.calls[-1][1]["result"] == "queue"
    for table in ("users", "join_requests", "attempts", "opt_outs"):
        assert await env.db.pool.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_opted_out_user_is_queued_and_nothing_is_stored(env):
    await env.db.mark_verified(USER, "captcha", -1)
    await env.gate.forget(USER, ignore=True)
    marker = await env.db.pool.fetchval("SELECT marker FROM opt_outs")
    assert marker == env.gate.optout_marker(USER) and str(USER).encode() not in marker

    env.bot.calls.clear()
    await env.gate.on_join_request(join_request())
    assert env.bot.calls == [("answer_chat_join_request_query", {"chat_join_request_query_id": "q-1", "result": "queue"})]
    assert await env.db.get_user(USER) is None
    assert await env.db.pool.fetchval("SELECT count(*) FROM join_requests") == 0


async def test_verify_me_turns_processing_back_on(env):
    await env.gate.forget(USER, ignore=True)
    _, data = await call(env, "challenge", "self")
    assert data["status"] == "challenge"
    assert not await env.gate.is_opted_out(USER)


async def test_times_round_trip_as_epoch_seconds(env):
    before = time.time()
    await env.db.mark_verified(USER, "manual", CHAT)
    user = await env.db.get_user(USER)
    assert before - 1 < user["verified_at"] < time.time() + 1


async def test_verify_me_also_approves_a_request_handed_to_the_admins(env):
    await env.gate.on_join_request(join_request())
    await call(env, "manual", 1)
    _, data = await call(env, "challenge", "self")
    attempt, stored = await latest_attempt(env)
    _, data = await call(env, "answer", "self", attempt=attempt, picked=answer_ids(stored))
    assert data["status"] == "verified_self" and data["approved"] == 1
    assert env.bot.calls[-1] == ("approve_chat_join_request", {"chat_id": CHAT, "user_id": USER})
    assert (await env.db.get_join_request(1))["outcome"] == "approved"


async def test_queued_request_an_admin_already_decided_stays_queued(env):
    from aiogram.exceptions import TelegramBadRequest

    await env.gate.on_join_request(join_request())
    await call(env, "manual", 1)

    async def gone(**kwargs):
        raise TelegramBadRequest(method=None, message="HIDE_REQUESTER_MISSING")

    env.bot.approve_chat_join_request = gone
    assert await env.gate.on_solved(USER, None) == 0
    assert (await env.db.get_join_request(1))["outcome"] == "queued"


async def test_mini_app_assets_are_versioned(env):
    res = await env.client.get("/app/")
    page = await res.text()
    assert 'src="static/app.js?v=' in page and 'href="static/style.css?v=' in page
