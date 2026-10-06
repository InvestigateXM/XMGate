from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from helpers import DATABASE_URL, TOKEN, FakeBot, fresh_db
from xmgate.api import setup_api
from xmgate.config import Config
from xmgate.flow import Gate


@pytest.fixture
async def env():
    """A Gate with a fake Bot, a wiped PostgreSQL, and the Mini App API on a test server."""
    cfg = Config(TOKEN, "https://gate.example", "s", "webhook", 300, DATABASE_URL, b"pepper", 0)
    db = await fresh_db()
    bot = FakeBot()
    gate = Gate(bot, db, cfg)
    app = web.Application()
    setup_api(app, gate)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield SimpleNamespace(gate=gate, bot=bot, db=db, client=client)
    await client.close()
    await db.close()
