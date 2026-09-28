import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from . import config as config_module
from .api import setup_api
from .db import DB
from .flow import Gate
from .handlers import build_router

ALLOWED_UPDATES = ["chat_join_request", "chat_member", "message"]

log = logging.getLogger("xmgate")


async def startup_checks(bot: Bot) -> None:
    me = await bot.get_me()
    log.info("Running as @%s", me.username)
    if not me.supports_join_request_queries:
        log.warning(
            "getMe says supports_join_request_queries is false: join requests will use the DM fallback, "
            "not the Mini App opening by itself. See README, 'Assign the bot to process join requests'."
        )


async def build_app() -> tuple[web.Application, Dispatcher, Bot, Gate]:
    cfg = config_module.load()
    bot = Bot(cfg.bot_token)
    db = await DB.open(cfg.db_path)
    gate = Gate(bot, db, cfg)
    dp = Dispatcher()
    dp.include_router(build_router(gate))

    app = web.Application()
    setup_api(app, gate)
    app.router.add_get("/health", lambda _: web.Response(text="ok"))
    return app, dp, bot, gate


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app, dp, bot, gate = await build_app()
    cfg = gate.config
    await startup_checks(bot)
    sweeper = asyncio.create_task(gate.run_sweeper())

    runner = web.AppRunner(app)
    if cfg.mode == "webhook":
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    log.info("Listening on :%s, Mini App at %s", cfg.port, cfg.app_url)

    try:
        if cfg.mode == "webhook":
            await bot.set_webhook(
                cfg.webhook_url,
                secret_token=cfg.webhook_secret,
                allowed_updates=ALLOWED_UPDATES,
                drop_pending_updates=True,
            )
            log.info("Webhook set to %s", cfg.webhook_url)
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=False)
    finally:
        sweeper.cancel()
        await runner.cleanup()
        await gate.db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
