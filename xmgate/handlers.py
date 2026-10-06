from aiogram import F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandStart
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message

from .flow import Gate


def build_router(gate: Gate) -> Router:
    router = Router()

    @router.chat_join_request()
    async def join_request(req: ChatJoinRequest) -> None:
        await gate.on_join_request(req)

    @router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
    async def member_joined(update: ChatMemberUpdated) -> None:
        await gate.on_member_joined(update.new_chat_member.user.id, update.chat.id)

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        await message.answer(
            "XM Gate prototype.\n\nRequest to join a test group that uses this bot and the captcha "
            "opens by itself. Send /reset to forget your verification and try it again."
        )

    @router.message(Command("reset"), F.chat.type == "private")
    async def reset(message: Message) -> None:
        await gate.db.forget_user(message.from_user.id)
        await message.answer("Done. Your next join request will show the captcha again.")

    return router
