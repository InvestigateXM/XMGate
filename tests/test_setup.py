"""Per-chat setup: the setup check, /link, the log chat and its lines. Same PostgreSQL setup as test_flow.py."""

import time
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError

from helpers import CHAT, USER, answer_ids, call, join_request, latest_attempt, needs_postgres
from xmgate import admin

pytestmark = needs_postgres

ADMIN = 2002
LOG = -100900
BOT_ID = 123456


def chat_info(**fields):
    base = dict(id=CHAT, type="supergroup", title="Test Ingress Chat", username="testingress", join_by_request=True,
                guard_bot=SimpleNamespace(id=BOT_ID, username="xmgatebot"))
    return SimpleNamespace(**{**base, **fields})


def member(status="administrator", can_invite_users=True):
    return SimpleNamespace(status=status, can_invite_users=can_invite_users)


def admins_of(*chats, user=ADMIN):
    """get_chat_member reply: the bot is an admin everywhere, `user` only in `chats`."""
    def reply(chat_id, user_id):
        if user_id == BOT_ID:
            return member()
        return member("administrator" if user_id == user and chat_id in chats else "member")
    return reply


async def with_log_chat(env):
    env.bot.replies["get_chat_member"] = admins_of(CHAT, LOG)
    await env.db.upsert_chat(CHAT, "supergroup", "Test Ingress Chat", "testingress")
    assert await admin.set_log_chat(env.gate, ADMIN, CHAT, LOG) == "Log chat saved. The bot posted a test message there."
    env.bot.calls.clear()


def log_lines(env):
    return [kw["text"] for name, kw in env.bot.calls if name == "send_message" and kw["chat_id"] == LOG]


async def test_setup_check_when_everything_is_in_place(env):
    env.bot.replies.update(get_chat=chat_info(), get_chat_member=member())
    text = await admin.setup_report(env.gate, CHAT)
    assert "❌" not in text and text.endswith("Everything is set up.")
    assert (await env.db.get_chat(CHAT))["has_invite_right"] is True


async def test_setup_check_lists_what_is_missing(env):
    env.bot.replies.update(get_chat=chat_info(join_by_request=False, guard_bot=None), get_chat_member=member(can_invite_users=False))
    text = await admin.setup_report(env.gate, CHAT)
    assert "❌ Make the XM Gate bot an admin" in text
    assert "❌ Turn on “Approve new members”" in text
    assert "❌ Assign the XM Gate bot to process join requests" in text


async def test_private_chat_gets_invite_link_hint_and_names_another_guard_bot(env):
    env.bot.replies.update(
        get_chat=chat_info(username=None, guard_bot=SimpleNamespace(id=1, username="otherbot")), get_chat_member=member()
    )
    text = await admin.setup_report(env.gate, CHAT)
    assert "request-to-join link" in text and "@otherbot" in text


async def test_promotion_posts_the_check_in_groups_and_dms_it_for_channels(env):
    env.bot.replies.update(get_chat=chat_info(), get_chat_member=member())
    update = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="Test Ingress Chat", username="testingress"),
        from_user=SimpleNamespace(id=ADMIN),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=member(),
    )
    where, text, _ = await admin.on_bot_status(env.gate, update)
    assert where == CHAT and "Everything is set up." in text

    update.chat.type = "channel"
    update.old_chat_member = SimpleNamespace(status="left")
    where, _, _ = await admin.on_bot_status(env.gate, update)
    assert where == ADMIN

    update.old_chat_member, update.new_chat_member = update.new_chat_member, SimpleNamespace(status="left")
    assert await admin.on_bot_status(env.gate, update) is None
    assert (await env.db.get_chat(CHAT))["is_active"] is False


async def test_same_rights_again_posts_nothing(env):
    update = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT, type="supergroup", title="T", username=None),
        from_user=SimpleNamespace(id=ADMIN),
        old_chat_member=member(),
        new_chat_member=member(),
    )
    assert await admin.on_bot_status(env.gate, update) is None


async def test_link_makes_a_request_to_join_invite(env):
    env.bot.replies["create_chat_invite_link"] = SimpleNamespace(invite_link="https://t.me/+abc")
    text = await admin.make_link(env.gate, CHAT)
    assert "https://t.me/+abc" in text
    assert env.bot.calls[-1] == (
        "create_chat_invite_link",
        {"chat_id": CHAT, "name": "XM Gate bot", "creates_join_request": True},
    )


async def test_admin_sees_only_chats_they_administer(env):
    await env.db.upsert_chat(CHAT, "supergroup", "Mine", None)
    await env.db.upsert_chat(-100777, "channel", "Not mine", None)
    env.bot.replies["get_chat_member"] = admins_of(CHAT)
    assert [c["chat_id"] for c in await admin.admin_chats(env.gate, ADMIN)] == [CHAT]


async def test_log_chat_needs_an_admin_of_both_chats(env):
    await env.db.upsert_chat(CHAT, "supergroup", "Test Ingress Chat", None)
    env.bot.replies["get_chat_member"] = admins_of(CHAT)
    assert "admin of both chats" in await admin.set_log_chat(env.gate, ADMIN, CHAT, LOG)
    assert (await env.db.get_chat(CHAT))["log_chat_id"] is None


async def test_log_chat_is_not_saved_when_the_bot_cannot_post_there(env):
    await env.db.upsert_chat(CHAT, "supergroup", "Test Ingress Chat", None)
    env.bot.replies["get_chat_member"] = admins_of(CHAT, LOG)

    def forbidden(**kwargs):
        raise TelegramForbiddenError(method=None, message="bot is not a member")

    env.bot.replies["send_message"] = forbidden
    assert "couldn't post there" in await admin.set_log_chat(env.gate, ADMIN, CHAT, LOG)
    assert (await env.db.get_chat(CHAT))["log_chat_id"] is None


async def test_log_line_for_a_verified_user(env):
    await with_log_chat(env)
    await env.db.mark_verified(USER, "captcha", -1)
    await env.db.pool.execute("UPDATE users SET verified_at = now() - interval '42 days'")
    await env.db.upsert_user(USER, "agent")
    await env.gate.on_join_request(join_request())
    assert log_lines(env) == ["Approved @agent in Test Ingress Chat. Passed the captcha 42 days ago in another chat."]


async def test_log_line_after_the_captcha(env):
    await with_log_chat(env)
    await env.gate.on_join_request(join_request())
    await call(env, "challenge", 1)
    attempt, stored = await latest_attempt(env)
    await call(env, "answer", 1, attempt=attempt, picked=answer_ids(stored))
    assert log_lines(env) == ["Approved @agent in Test Ingress Chat. Passed the captcha just now."]


async def test_log_lines_for_ask_an_admin_and_timeout(env):
    await with_log_chat(env)
    await env.gate.on_join_request(join_request())
    await call(env, "manual", 1)
    await env.gate.on_join_request(join_request(query_id="q-2"))
    await env.db.update_join_request(2, grace_until=time.time() - 1)
    await env.gate.sweep()
    assert log_lines(env) == [
        "@agent asked for a chat admin to approve their request to join Test Ingress Chat.",
        "Declined @agent in Test Ingress Chat. The captcha was not finished in time.",
    ]


async def test_no_log_line_for_an_opted_out_user(env):
    await with_log_chat(env)
    await env.gate.forget(USER, ignore=True)
    await env.gate.on_join_request(join_request())
    assert log_lines(env) == []


async def test_user_without_username_is_linked_by_id(env):
    await with_log_chat(env)
    req = join_request()
    req.from_user.username = None
    await env.gate.on_join_request(req)
    await call(env, "manual", 1)
    assert log_lines(env)[0].startswith(f'<a href="tg://user?id={USER}">user {USER}</a> asked')
