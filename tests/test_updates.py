from types import SimpleNamespace

from aiogram import Dispatcher

from xmgate.handlers import build_router


def test_bot_asks_telegram_for_every_update_type_it_handles():
    dp = Dispatcher()
    dp.include_router(build_router(SimpleNamespace()))
    assert set(dp.resolve_used_update_types()) == {
        "chat_join_request",
        "chat_member",
        "my_chat_member",
        "message",
        "callback_query",
    }
