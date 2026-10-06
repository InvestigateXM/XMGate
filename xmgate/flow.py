"""Join request outcomes shared by the bot handlers, the Mini App API and the sweeper."""

import asyncio
import hashlib
import hmac
import html
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, WebAppInfo

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

SWEEP_EVERY = 15
ADMIN_STATUSES = ("creator", "administrator")


def who(user_id: int, username: str | None) -> str:
    """HTML mention for log lines: @username when known, otherwise a link by id."""
    if username:
        return f"@{html.escape(username)}"
    return f'<a href="tg://user?id={user_id}">user {user_id}</a>'


def _ago(ts: float) -> str:
    days = int((time.time() - ts) // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


class Gate:
    def __init__(self, bot: Bot, db: DB, config: Config):
        self.bot = bot
        self.db = db
        self.config = config
        self._key = hmac.new(b"xmgate-request-link", config.bot_token.encode(), hashlib.sha256).digest()
        self.bot_id = int(config.bot_token.split(":", 1)[0])
        self.username: str | None = None  # set at startup from getMe, used for t.me deep links

    # Signed links from the Mini App back to its join request, or to "verify me" from the DM menu

    def _sign(self, subject: str) -> str:
        return hmac.new(self._key, subject.encode(), hashlib.sha256).hexdigest()[:32]

    def request_token(self, jr_id: int, user_id: int) -> str:
        return f"{jr_id}.{self._sign(f'{jr_id}:{user_id}')}"

    def self_token(self, user_id: int) -> str:
        return f"self.{self._sign(f'self:{user_id}')}"

    def parse_token(self, token: str, user_id: int) -> tuple[bool, int | None]:
        """(valid, join request id). A valid self-verification token has no join request."""
        head, _, _ = token.partition(".")
        if head == "self":
            return hmac.compare_digest(self.self_token(user_id), token), None
        if not head.isdigit():
            return False, None
        return hmac.compare_digest(self.request_token(int(head), user_id), token), int(head)

    def app_url(self, jr_id: int, user_id: int) -> str:
        return f"{self.config.app_url}?r={self.request_token(jr_id, user_id)}"

    def self_verify_url(self, user_id: int) -> str:
        return f"{self.config.app_url}?r={self.self_token(user_id)}"

    # Opt-out marker (design §9): keyed hash, so a database dump alone can't reveal the ids

    def optout_marker(self, user_id: int) -> bytes:
        return hmac.new(self.config.optout_pepper, str(user_id).encode(), hashlib.sha256).digest()

    async def is_opted_out(self, user_id: int) -> bool:
        return await self.db.is_opted_out(self.optout_marker(user_id))

    # Incoming join request (design §3)

    async def on_join_request(self, req) -> None:
        user = req.from_user
        chat = req.chat
        # Join request updates only reach admins with the invite right, so this also records that.
        await self.db.upsert_chat(chat.id, chat.type, chat.title, chat.username, is_active=True, has_invite_right=True)
        if await self.is_opted_out(user.id):
            # Store and log nothing about this user; hand the request to the admins.
            if req.query_id:
                try:
                    await self.bot.answer_chat_join_request_query(chat_join_request_query_id=req.query_id, result="queue")
                except TelegramAPIError as e:
                    log.warning("opted-out join request in %s: could not queue: %s", chat.id, e)
            log.info("join request in %s from an opted-out user, left for the admins", chat.id)
            return
        await self.db.upsert_user(user.id, user.username)
        known = await self.db.get_user(user.id)
        path = "query" if req.query_id else "dm"
        jr_id = await self.db.add_join_request(
            chat_id=chat.id,
            chat_title=chat.title,
            chat_username=chat.username,
            tg_user_id=user.id,
            user_chat_id=req.user_chat_id,
            path=path,
            query_id=req.query_id,
        )
        jr = await self.db.get_join_request(jr_id)

        if known["verified_at"]:
            log.info("jr %s: user %s already verified, approving", jr_id, user.id)
            if await self.resolve(jr, "approve"):
                how = "Passed the captcha" if known["verified_method"] == "captcha" else "Approved by a chat admin"
                where = "this chat" if known["verified_chat"] == chat.id else "another chat"
                await self.note(jr, f"Approved {{who}} in {{chat}}. {how} {_ago(known['verified_at'])} in {where}.")
            return

        if path == "query":
            log.info("jr %s: opening captcha Mini App for user %s in %s", jr_id, user.id, chat.id)
            await self.db.update_join_request(jr_id, outcome="captcha", last_heartbeat=time.time())
            try:
                await self.bot.send_chat_join_request_web_app(
                    chat_join_request_query_id=req.query_id, web_app_url=self.app_url(jr_id, user.id)
                )
            except TelegramAPIError as e:
                log.warning("jr %s: sendChatJoinRequestWebApp failed (%s), queueing", jr_id, e)
                if await self.resolve(await self.db.get_join_request(jr_id), "queue"):
                    await self.note(jr, "The captcha could not be opened for {who}, so their request to join {chat} waits for an admin.")
            return

        # DM fallback (design §4): the bot is not this chat's join-request processor.
        log.info("jr %s: no query_id, sending DM to user %s", jr_id, user.id)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Verify me", web_app=WebAppInfo(url=self.app_url(jr_id, user.id)))]]
        )
        try:
            msg = await self.bot.send_message(
                req.user_chat_id,
                f"You asked to join {chat.title}. Tap the button to prove you're not a bot. "
                f"You have {self.config.grace_seconds // 60} minutes.",
                reply_markup=keyboard,
            )
        except TelegramAPIError as e:
            log.info("jr %s: DM failed (%s), leaving it for the admins", jr_id, e)
            if await self.db.claim_join_request(jr_id, "queued"):
                await self.note(jr, "The XM Gate bot could not message {who}, so their request to join {chat} waits for an admin.")
            return
        await self.db.update_join_request(
            jr_id, dm_message_id=msg.message_id, grace_until=time.time() + self.config.grace_seconds
        )

    # Final answers

    async def resolve(self, jr, result: str) -> bool:
        """Answers one join request with approve, decline or queue. Returns False if it was already resolved."""
        outcome = {"approve": "approved", "decline": "declined_timeout", "queue": "queued"}[result]
        if not await self.db.claim_join_request(jr["id"], outcome):
            return False
        try:
            if jr["path"] == "query":
                try:
                    await self.bot.answer_chat_join_request_query(
                        chat_join_request_query_id=jr["query_id"], result=result
                    )
                except TelegramAPIError as e:
                    # The query may have expired while the app was open (design §12 open question).
                    # approve/decline by chat and user still works while the request is pending.
                    log.warning("jr %s: answerChatJoinRequestQuery(%s) failed: %s", jr["id"], result, e)
                    if result == "queue":
                        raise
                    await self._by_chat(jr, result)
            elif result != "queue":
                await self._by_chat(jr, result)
            log.info("jr %s: %s", jr["id"], outcome)
        except TelegramAPIError as e:
            log.warning("jr %s: could not %s: %s", jr["id"], result, e)
        return True

    async def _by_chat(self, jr, result: str) -> None:
        method = self.bot.approve_chat_join_request if result == "approve" else self.bot.decline_chat_join_request
        await method(chat_id=jr["chat_id"], user_id=jr["tg_user_id"])

    async def on_solved(self, user_id: int, chat_id: int | None) -> int:
        """Marks the user verified and approves every request still waiting in any chat. Returns how many."""
        await self.db.mark_verified(user_id, "captcha", chat_id)
        approved = 0
        for jr in await self.db.waiting_requests_for_user(user_id):
            if jr["outcome"] == "queued":
                ok = await self._approve_queued(jr)
            else:
                ok = await self.resolve(jr, "approve")
            if ok:
                approved += 1
                await self.note(jr, "Approved {who} in {chat}. Passed the captcha just now.")
                if jr["dm_message_id"]:
                    await self._edit_dm(jr, f"Verified. Welcome to {jr['chat_title']}.", keep_button=False)
        return approved

    async def _approve_queued(self, jr) -> bool:
        """A request already handed to the admins is still pending in Telegram, so the bot can approve it itself."""
        try:
            await self._by_chat(jr, "approve")
        except TelegramAPIError as e:
            # Most likely an admin already decided it.
            log.info("jr %s: could not approve the queued request: %s", jr["id"], e)
            return False
        log.info("jr %s: approved after the user verified", jr["id"])
        return await self.db.claim_join_request(jr["id"], "approved", from_outcomes=("queued",))

    async def hand_to_admins(self, jr) -> None:
        """The user tapped "Ask an admin to approve me instead"."""
        if await self.resolve(jr, "queue"):
            await self.note(jr, "{who} asked for a chat admin to approve their request to join {chat}.")

    async def forget(self, user_id: int, ignore: bool) -> None:
        """Delete my data, or delete and opt out when ignore is set (design §9)."""
        # Open requests would otherwise hang until they expire: hand them to the admins first.
        for jr in await self.db.open_requests_for_user(user_id):
            await self.resolve(jr, "queue")
        await self.db.forget_user(user_id)
        if ignore:
            await self.db.add_opt_out(self.optout_marker(user_id))
        log.info("a user deleted their data%s", " and opted out" if ignore else "")

    async def on_member_joined(self, user_id: int, chat_id: int) -> None:
        """An admin approved a request the bot had queued: count it as manual verification."""
        jr = await self.db.queued_request(user_id, chat_id)
        if jr and await self.db.claim_join_request(jr["id"], "admin_approved", from_outcomes=("queued",)):
            await self.db.mark_verified(user_id, "manual", chat_id)
            log.info("jr %s: admin approved, user %s marked verified (manual)", jr["id"], user_id)

    # 5-minute grace period (design §5)

    async def sweep(self) -> None:
        for jr in await self.db.expired_requests(time.time(), self.config.grace_seconds):
            if not await self.resolve(jr, "decline"):
                continue
            await self.note(jr, "Declined {who} in {chat}. The captcha was not finished in time.")
            if jr["dm_message_id"]:
                await self._edit_dm(
                    jr,
                    f"Your request to join {jr['chat_title']} timed out. You can still verify with the "
                    "button below, and your next request will be approved straight away.",
                    keep_button=True,
                )

    async def run_sweeper(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("sweeper failed")
            await asyncio.sleep(SWEEP_EVERY)

    # Chat admins and the optional log chat (design §2, §3)

    async def is_admin(self, chat_id: int, user_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except TelegramAPIError:
            return False
        return member.status in ADMIN_STATUSES

    async def note(self, jr, line: str) -> None:
        """Posts one line about a join request to the chat's log chat, if it has one.

        {who} and {chat} in the line are filled in with the user and the chat's title.
        """
        chat = await self.db.get_chat(jr["chat_id"])
        if not chat or not chat["log_chat_id"]:
            return
        user = await self.db.get_user(jr["tg_user_id"])
        text = line.format(
            who=who(jr["tg_user_id"], user["last_username"] if user else None),
            chat=html.escape(jr["chat_title"] or chat["title"] or str(jr["chat_id"])),
        )
        try:
            await self.bot.send_message(
                chat_id=chat["log_chat_id"],
                text=text,
                parse_mode="HTML",
                disable_notification=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramAPIError as e:
            log.warning("chat %s: could not post to its log chat %s: %s", jr["chat_id"], chat["log_chat_id"], e)

    async def _edit_dm(self, jr, text: str, keep_button: bool) -> None:
        markup = None
        if keep_button:
            url = self.app_url(jr["id"], jr["tg_user_id"])
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Verify me", web_app=WebAppInfo(url=url))]]
            )
        try:
            await self.bot.edit_message_text(
                text=text, chat_id=jr["user_chat_id"], message_id=jr["dm_message_id"], reply_markup=markup
            )
        except TelegramAPIError as e:
            log.info("jr %s: could not edit DM: %s", jr["id"], e)
