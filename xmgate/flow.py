"""Join request outcomes shared by the bot handlers, the Mini App API and the sweeper."""

import asyncio
import hashlib
import hmac
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

SWEEP_EVERY = 15


class Gate:
    def __init__(self, bot: Bot, db: DB, config: Config):
        self.bot = bot
        self.db = db
        self.config = config
        self._key = hmac.new(b"xmgate-request-link", config.bot_token.encode(), hashlib.sha256).digest()

    # Signed link from the Mini App back to its join request

    def request_token(self, jr_id: int, user_id: int) -> str:
        sig = hmac.new(self._key, f"{jr_id}:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{jr_id}.{sig}"

    def check_token(self, token: str, user_id: int) -> int | None:
        jr_id, _, sig = token.partition(".")
        if not jr_id.isdigit():
            return None
        return int(jr_id) if hmac.compare_digest(self.request_token(int(jr_id), user_id), token) else None

    def app_url(self, jr_id: int, user_id: int) -> str:
        return f"{self.config.app_url}?r={self.request_token(jr_id, user_id)}"

    # Incoming join request (design §3)

    async def on_join_request(self, req) -> None:
        user = req.from_user
        chat = req.chat
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
            await self.resolve(jr, "approve")
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
                await self.resolve(await self.db.get_join_request(jr_id), "queue")
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
            await self.db.claim_join_request(jr_id, "queued")
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
        """Marks the user verified and approves every open request they have. Returns how many."""
        await self.db.mark_verified(user_id, "captcha", chat_id)
        approved = 0
        for jr in await self.db.open_requests_for_user(user_id):
            if await self.resolve(jr, "approve"):
                approved += 1
                if jr["dm_message_id"]:
                    await self._edit_dm(jr, f"Verified. Welcome to {jr['chat_title']}.", keep_button=False)
        return approved

    async def on_member_joined(self, user_id: int, chat_id: int) -> None:
        """An admin approved a request the bot had queued: count it as manual verification."""
        jr = await self.db.queued_request(user_id, chat_id)
        if jr and await self.db.claim_join_request(jr["id"], "admin_approved", from_outcomes=("queued",)):
            await self.db.mark_verified(user_id, "manual", chat_id)
            log.info("jr %s: admin approved, user %s marked verified (manual)", jr["id"], user_id)

    # 5-minute grace period (design §5)

    async def sweep(self) -> None:
        for jr in await self.db.expired_requests(time.time(), self.config.grace_seconds):
            if await self.resolve(jr, "decline") and jr["dm_message_id"]:
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
