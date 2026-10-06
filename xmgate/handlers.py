import time

from aiogram import F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from .flow import Gate

INTRO = (
    "The XM Gate bot checks that people asking to join Ingress chats aren't automated spam accounts. "
    "Once you have passed the captcha, all chats that use this bot will automatically approve you in the future."
)
OPTED_OUT = (
    "You asked not to process your data. Your join requests will go straight to each chat's admins, "
    "and all we keep is an anonymous marker so the bot knows to ignore you.\n\n"
    "Tap Verify me if you'd like automatic approval again. This will clear the marker and turns processing back on."
)


def _date(ts: float) -> str:
    return time.strftime("%-d %b %Y", time.gmtime(ts))


def build_router(gate: Gate) -> Router:
    router = Router()
    private = F.chat.type == "private"

    def verify_button(user_id: int) -> InlineKeyboardButton:
        return InlineKeyboardButton(text="Verify me", web_app=WebAppInfo(url=gate.self_verify_url(user_id)))

    def back() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="me:menu")]])

    async def menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        if await gate.is_opted_out(user_id):
            return OPTED_OUT, InlineKeyboardMarkup(inline_keyboard=[[verify_button(user_id)]])
        user = await gate.db.get_user(user_id)
        if user and user["verified_at"]:
            status = f"Status: verified on {_date(user['verified_at'])}."
        else:
            status = "Status: not verified yet."
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [verify_button(user_id), InlineKeyboardButton(text="My data", callback_data="me:data")],
                [InlineKeyboardButton(text="Delete my data", callback_data="me:delete")],
                [InlineKeyboardButton(text="Delete and ignore me", callback_data="me:ignore")],
            ]
        )
        return f"{INTRO}\n\n{status}", keyboard

    async def my_data(user_id: int) -> str:
        row = await gate.db.user_summary(user_id)
        if row is None:
            return "There is currently no data stored about you."
        lines = ["This is everything the bot stores about you:", "", f"Telegram id: {row['tg_user_id']}"]
        if row["last_username"]:
            lines.append(f"Username when last seen: @{row['last_username']}")
        lines.append(f"First seen: {_date(row['first_seen_at'])}")
        if row["verified_at"]:
            how = "passed the captcha" if row["verified_method"] == "captcha" else "approved by a chat admin"
            where = f" in {row['verified_chat_title']}" if row["verified_chat_title"] else ""
            lines.append(f"Verified: {_date(row['verified_at'])}, {how}{where}")
        else:
            lines.append("Verified: no")
        lines.append(f"Join requests: {row['requests']} ({row['open_requests']} still open)")
        lines.append(f"Captcha attempts: {row['attempts']}")
        return "\n".join(lines)

    @router.chat_join_request()
    async def join_request(req: ChatJoinRequest) -> None:
        await gate.on_join_request(req)

    @router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
    async def member_joined(update: ChatMemberUpdated) -> None:
        await gate.on_member_joined(update.new_chat_member.user.id, update.chat.id)

    # DM menu (design §9). Any private message shows it.

    @router.message(private)
    async def show_menu(message: Message) -> None:
        text, keyboard = await menu(message.from_user.id)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("me:"))
    async def menu_action(cb: CallbackQuery) -> None:
        user_id = cb.from_user.id
        action = cb.data.removeprefix("me:")
        if action == "menu":
            text, keyboard = await menu(user_id)
        elif action == "data":
            text, keyboard = await my_data(user_id), back()
        elif action == "delete":
            text = (
                "Delete all data this bot has stored? Next time you ask to join a chat that uses the bot, "
                "you'll see the captcha again."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Delete", callback_data="me:delete:yes")],
                    [InlineKeyboardButton(text="Cancel", callback_data="me:menu")],
                ]
            )
        elif action == "ignore":
            text = (
                "Delete everything and stop processing you? Your join requests will go to each chat's admins "
                "instead of being approved automatically. Only an anonymous marker will be kept so the bot knows to ignore you."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Delete and ignore me", callback_data="me:ignore:yes")],
                    [InlineKeyboardButton(text="Cancel", callback_data="me:menu")],
                ]
            )
        elif action == "delete:yes":
            await gate.forget(user_id, ignore=False)
            text, keyboard = "Done. All data has been deleted.", back()
        elif action == "ignore:yes":
            await gate.forget(user_id, ignore=True)
            text, keyboard = await menu(user_id)
            text = "Done. All data has been deleted and you will be ignored by the bot from now on.\n\n" + text
        else:
            await cb.answer()
            return
        await cb.answer()
        if cb.message:
            await cb.message.edit_text(text, reply_markup=keyboard)

    return router
