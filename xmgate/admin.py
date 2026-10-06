"""Per-chat setup (design §2): the setup check, /link, and the optional log chat.

In groups, admins use /setup and /link. Channels can't take commands, so every chat can
also be managed from a DM with the bot: /admin lists the chats the sender administers.
"""

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .flow import Gate

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("member", "administrator", "restricted")
LINK_NAME = "XM Gate bot"
PICK_CHANNEL, PICK_GROUP = 1, 2


async def setup_report(gate: Gate, chat_id: int) -> str:
    """What the chat still needs before the bot can check join requests, one line per item."""
    try:
        chat = await gate.bot.get_chat(chat_id=chat_id)
        me = await gate.bot.get_chat_member(chat_id=chat_id, user_id=gate.bot_id)
    except TelegramAPIError as e:
        log.info("chat %s: setup check failed: %s", chat_id, e)
        return "The XM Gate bot can't see this chat any more. Add it again as an admin to use it here."
    title = html.escape(chat.title or str(chat_id))
    is_admin = me.status == "administrator"
    can_invite = is_admin and bool(me.can_invite_users)
    await gate.db.upsert_chat(chat_id, chat.type, chat.title, chat.username, is_active=True, has_invite_right=can_invite)

    lines = [f"<b>XM Gate bot setup for {title}</b>", ""]
    missing = 0
    if can_invite:
        lines.append("✅ The bot is an admin and can invite users via link.")
    else:
        missing += 1
        lines.append("❌ Make the XM Gate bot an admin with the right to invite users via link.")

    if chat.type == "supergroup" and chat.username:
        if chat.join_by_request:
            lines.append("✅ New members need approval to join.")
        else:
            missing += 1
            lines.append("❌ Turn on “Approve new members” in the group settings.")
    else:
        lines.append("ℹ️ People join this chat through invite links. Share a request-to-join link, which the bot can make for you.")

    if is_admin:
        guard = chat.guard_bot
        if guard and guard.id == gate.bot_id:
            lines.append("✅ The bot processes join requests, so the captcha opens by itself.")
        else:
            missing += 1
            other = f" Another bot (@{html.escape(guard.username)}) does this now." if guard and guard.username else ""
            lines.append(
                "❌ Assign the XM Gate bot to process join requests in the chat settings." + other
                + " Until then, the bot sends the captcha by DM instead."
            )

    lines.append("")
    lines.append("Everything is set up." if not missing else "Tap Check again once you have changed the settings.")
    return "\n".join(lines)


async def make_link(gate: Gate, chat_id: int) -> str:
    try:
        link = await gate.bot.create_chat_invite_link(chat_id=chat_id, name=LINK_NAME, creates_join_request=True)
    except TelegramAPIError as e:
        log.info("chat %s: could not create an invite link: %s", chat_id, e)
        return "The XM Gate bot couldn't make a link. It needs to be an admin with the right to invite users via link."
    return (
        f"Request-to-join link:\n{link.invite_link}\n\n"
        "Everyone who joins through it goes through the XM Gate bot's check first."
    )


async def admin_chats(gate: Gate, user_id: int) -> list:
    return [chat for chat in await gate.db.active_chats() if await gate.is_admin(chat["chat_id"], user_id)]


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def panel(gate: Gate, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """The settings for one chat, shown in a DM with an admin."""
    text = await setup_report(gate, chat_id)
    chat = await gate.db.get_chat(chat_id)
    rows = [[_button("Check again", f"adm:open:{chat_id}")], [_button("Request-to-join link", f"adm:link:{chat_id}")]]
    if chat and chat["log_chat_id"]:
        log_title = await _chat_title(gate, chat["log_chat_id"])
        text += f"\n\nLog chat: {html.escape(log_title)}. The bot posts one line there for each join request it decides."
        rows.append([_button("Change log chat", f"adm:log:{chat_id}"), _button("Turn off", f"adm:logoff:{chat_id}")])
    else:
        text += "\n\nLog chat: none. The bot can post one line to a group or channel for each join request it decides."
        rows.append([_button("Set a log chat", f"adm:log:{chat_id}")])
    rows.append([_button("All my chats", "adm:list:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def chat_list(gate: Gate, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    chats = await admin_chats(gate, user_id)
    if not chats:
        return (
            "The XM Gate bot isn't in any chat you administer yet. Add it to your group or channel as an admin "
            "with the right to invite users via link, then send /admin again.",
            None,
        )
    rows = [[_button(chat["title"] or str(chat["chat_id"]), f"adm:open:{chat['chat_id']}")] for chat in chats]
    return "Which chat would you like to set up?", InlineKeyboardMarkup(inline_keyboard=rows)


async def _chat_title(gate: Gate, chat_id: int) -> str:
    try:
        return (await gate.bot.get_chat(chat_id=chat_id)).title or str(chat_id)
    except TelegramAPIError:
        return f"chat {chat_id} (the bot can't see it any more)"


async def set_log_chat(gate: Gate, user_id: int, chat_id: int, log_chat_id: int) -> str:
    """Checks the admin runs both chats and the bot can post in the log chat, then saves it."""
    if not await gate.is_admin(chat_id, user_id) or not await gate.is_admin(log_chat_id, user_id):
        return "You need to be an admin of both chats to do this."
    chat = await gate.db.get_chat(chat_id)
    title = html.escape(chat["title"] if chat and chat["title"] else str(chat_id))
    try:
        await gate.bot.send_message(
            chat_id=log_chat_id,
            text=f"The XM Gate bot will post join request decisions for {title} here.",
            parse_mode="HTML",
            disable_notification=True,
        )
    except TelegramAPIError as e:
        log.info("chat %s: test post to log chat %s failed: %s", chat_id, log_chat_id, e)
        return "The XM Gate bot couldn't post there. Add it to that chat with permission to post messages, then try again."
    await gate.db.set_log_chat(chat_id, log_chat_id)
    log.info("chat %s: log chat set to %s", chat_id, log_chat_id)
    return "Log chat saved. The bot posted a test message there."


async def on_bot_status(gate: Gate, update: ChatMemberUpdated) -> tuple[int, str, InlineKeyboardMarkup | None] | None:
    """Records the bot's own status in a chat. Returns where and what to post as the setup check, if anything."""
    chat = update.chat
    new, old = update.new_chat_member, update.old_chat_member
    active = new.status in ACTIVE_STATUSES
    can_invite = new.status == "administrator" and bool(getattr(new, "can_invite_users", False))
    await gate.db.upsert_chat(chat.id, chat.type, chat.title, chat.username, is_active=active, has_invite_right=can_invite)
    log.info("chat %s: bot is now %s (invite right: %s)", chat.id, new.status, can_invite)
    if not active:
        return None
    rights_changed = getattr(old, "can_invite_users", None) != getattr(new, "can_invite_users", None)
    if old.status == new.status and not rights_changed:
        return None
    if chat.type == "channel":
        # Posting in a channel would reach every subscriber, so the admin who added the bot gets it by DM.
        text, keyboard = await panel(gate, chat.id)
        return update.from_user.id, text, keyboard
    text = await setup_report(gate, chat.id)
    return chat.id, text, group_keyboard(gate, chat.id)


def group_keyboard(gate: Gate, chat_id: int) -> InlineKeyboardMarkup:
    rows = [[_button("Check again", f"setup:{chat_id}")]]
    if gate.username:
        url = f"https://t.me/{gate.username}?start=admin_{chat_id}"
        rows.append([InlineKeyboardButton(text="More settings (in a DM)", url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_admin_router(gate: Gate) -> Router:
    router = Router()
    groups = F.chat.type.in_({"group", "supergroup"})
    private = F.chat.type == "private"
    picking: dict[int, int] = {}  # admin's user id -> chat waiting for its log chat

    async def sender_is_admin(message: Message) -> bool:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True  # an anonymous admin
        return bool(message.from_user) and await gate.is_admin(message.chat.id, message.from_user.id)

    async def send(chat_id: int, text: str, keyboard=None) -> None:
        try:
            await gate.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard)
        except TelegramAPIError as e:
            log.info("could not send the setup check to %s: %s", chat_id, e)

    @router.my_chat_member()
    async def bot_status(update: ChatMemberUpdated) -> None:
        if update.chat.type == "private":
            return
        post = await on_bot_status(gate, update)
        if post:
            await send(*post)

    @router.message(groups, Command("setup"))
    async def setup_command(message: Message) -> None:
        if await sender_is_admin(message):
            await send(message.chat.id, await setup_report(gate, message.chat.id), group_keyboard(gate, message.chat.id))

    @router.message(groups, Command("link"))
    async def link_command(message: Message) -> None:
        if await sender_is_admin(message):
            await message.reply(await make_link(gate, message.chat.id))

    @router.callback_query(F.data.startswith("setup:"))
    async def setup_check_again(cb: CallbackQuery) -> None:
        chat_id = int(cb.data.removeprefix("setup:"))
        if not await gate.is_admin(chat_id, cb.from_user.id):
            await cb.answer("Only chat admins can do this.", show_alert=True)
            return
        await cb.answer()
        if cb.message:
            try:
                await cb.message.edit_text(
                    await setup_report(gate, chat_id), parse_mode="HTML", reply_markup=group_keyboard(gate, chat_id)
                )
            except TelegramAPIError:
                pass  # nothing changed since the last check

    # DM: settings for any chat the sender administers, channels included

    @router.message(private, Command("admin"))
    async def admin_command(message: Message) -> None:
        text, keyboard = await chat_list(gate, message.from_user.id)
        await message.answer(text, reply_markup=keyboard)

    @router.message(private, CommandStart(deep_link=True, magic=F.args.startswith("admin_")))
    async def admin_deep_link(message: Message, command: CommandObject) -> None:
        try:
            chat_id = int(command.args.removeprefix("admin_"))
        except ValueError:
            return
        if not await gate.is_admin(chat_id, message.from_user.id):
            await message.answer("You need to be an admin of that chat to change its settings.")
            return
        text, keyboard = await panel(gate, chat_id)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("adm:"))
    async def admin_action(cb: CallbackQuery) -> None:
        _, action, raw_id = cb.data.split(":", 2)
        chat_id = int(raw_id)
        user_id = cb.from_user.id
        if action == "list":
            await cb.answer()
            text, keyboard = await chat_list(gate, user_id)
            await cb.message.edit_text(text, reply_markup=keyboard)
            return
        if not await gate.is_admin(chat_id, user_id):
            await cb.answer("You're no longer an admin of that chat.", show_alert=True)
            return
        await cb.answer()
        if action == "link":
            await cb.message.answer(await make_link(gate, chat_id))
            return
        if action == "log":
            picking[user_id] = chat_id
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="Pick a channel",
                            request_chat=KeyboardButtonRequestChat(request_id=PICK_CHANNEL, chat_is_channel=True, bot_is_member=True),
                        ),
                        KeyboardButton(
                            text="Pick a group",
                            request_chat=KeyboardButtonRequestChat(request_id=PICK_GROUP, chat_is_channel=False, bot_is_member=True),
                        ),
                    ],
                    [KeyboardButton(text="Cancel")],
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await cb.message.answer(
                "Pick the group or channel for the log lines. The XM Gate bot needs to be a member there "
                "and allowed to post messages.",
                reply_markup=keyboard,
            )
            return
        if action == "logoff":
            await gate.db.set_log_chat(chat_id, None)
        text, keyboard = await panel(gate, chat_id)
        try:
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except TelegramAPIError:
            pass  # nothing changed since the last check

    @router.message(private, F.chat_shared)
    async def log_chat_picked(message: Message) -> None:
        chat_id = picking.pop(message.from_user.id, None)
        if chat_id is None:
            await message.answer("Please start again with /admin.", reply_markup=ReplyKeyboardRemove())
            return
        result = await set_log_chat(gate, message.from_user.id, chat_id, message.chat_shared.chat_id)
        await message.answer(result, reply_markup=ReplyKeyboardRemove())
        text, keyboard = await panel(gate, chat_id)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @router.message(private, F.text == "Cancel", F.from_user.id.func(lambda uid: uid in picking))
    async def log_chat_cancelled(message: Message) -> None:
        picking.pop(message.from_user.id, None)
        await message.answer("No changes made.", reply_markup=ReplyKeyboardRemove())

    return router
