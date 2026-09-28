"""HTTP API the captcha Mini App talks to. Every call carries initData (design §6)."""

import json
import logging
import secrets
import time
from pathlib import Path

from aiohttp import web

from . import captcha
from .db import OPEN
from .flow import Gate
from .initdata import InitDataError, validate_init_data

log = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"

CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; img-src 'self' data:; "
    "style-src 'self'; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
)


class Ctx:
    def __init__(self, user_id: int, jr):
        self.user_id = user_id
        self.jr = jr


async def _auth(request: web.Request, gate: Gate) -> tuple[Ctx, dict]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("tma "):
        raise web.HTTPUnauthorized(text="missing initData")
    try:
        init = validate_init_data(header[4:], gate.config.bot_token)
    except InitDataError as e:
        raise web.HTTPUnauthorized(text=str(e))
    user_id = int(init["user"]["id"])
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="bad json")
    jr_id = gate.check_token(str(body.get("r", "")), user_id)
    if jr_id is None:
        raise web.HTTPForbidden(text="this link is not for you")
    jr = await gate.db.get_join_request(jr_id)
    if jr is None:
        raise web.HTTPNotFound(text="request not found")
    return Ctx(user_id, jr), body


def _chat_link(jr) -> str | None:
    return f"https://t.me/{jr['chat_username']}" if jr["chat_username"] else None


def setup_api(app: web.Application, gate: Gate) -> None:
    db = gate.db

    async def challenge(request: web.Request) -> web.Response:
        ctx, _ = await _auth(request, gate)
        jr = ctx.jr
        base = {"chat_title": jr["chat_title"], "chat_link": _chat_link(jr)}
        if jr["outcome"] in ("approved", "admin_approved"):
            return web.json_response({**base, "status": "approved"})
        user = await db.get_user(ctx.user_id)
        if user and user["verified_at"]:
            if jr["outcome"] in OPEN:
                await gate.resolve(jr, "approve")
                return web.json_response({**base, "status": "approved"})
            return web.json_response({**base, "status": "verified_late"})
        if jr["outcome"] == "queued":
            return web.json_response({**base, "status": "queued"})

        misses = await db.recent_misses(ctx.user_id, time.time() - captcha.MISS_WINDOW)
        wait = captcha.wait_seconds(misses)
        suggest_manual = len(misses) >= captcha.SUGGEST_MANUAL_AFTER
        if wait:
            return web.json_response({**base, "status": "wait", "wait": wait, "suggest_manual": suggest_manual})

        stored, public = captcha.new_challenge()
        attempt_id = secrets.token_urlsafe(12)
        await db.add_attempt(attempt_id, ctx.user_id, jr["id"], stored)
        return web.json_response(
            {
                **base,
                "status": "challenge",
                "attempt": attempt_id,
                "tiles": public,
                "columns": captcha.COLUMNS,
                "misses": len(misses),
                "suggest_manual": suggest_manual,
                "request_open": jr["outcome"] in OPEN,
            }
        )

    async def answer(request: web.Request) -> web.Response:
        ctx, body = await _auth(request, gate)
        attempt = await db.get_attempt(str(body.get("attempt", "")))
        if attempt is None or attempt["tg_user_id"] != ctx.user_id or attempt["join_request_id"] != ctx.jr["id"]:
            raise web.HTTPNotFound(text="unknown attempt")
        picked = [str(p) for p in body.get("picked", [])][:captcha.TILE_COUNT]
        correct = captcha.is_correct(json.loads(attempt["tiles"]), picked)
        solve_ms = body.get("solve_ms")
        if not await db.answer_attempt(
            attempt["id"], correct, int(solve_ms) if isinstance(solve_ms, (int, float)) else None, bool(body.get("had_touch"))
        ):
            raise web.HTTPConflict(text="attempt already answered")
        base = {"chat_title": ctx.jr["chat_title"], "chat_link": _chat_link(ctx.jr)}
        if not correct:
            log.info("jr %s: wrong answer from user %s", ctx.jr["id"], ctx.user_id)
            return web.json_response({**base, "status": "wrong"})
        await gate.on_solved(ctx.user_id, ctx.jr["chat_id"])
        jr = await db.get_join_request(ctx.jr["id"])
        status = "approved" if jr["outcome"] == "approved" else "verified_late"
        return web.json_response({**base, "status": status})

    async def manual(request: web.Request) -> web.Response:
        ctx, _ = await _auth(request, gate)
        if ctx.jr["outcome"] in OPEN:
            await gate.resolve(ctx.jr, "queue")
        jr = await db.get_join_request(ctx.jr["id"])
        return web.json_response({"status": "queued" if jr["outcome"] == "queued" else jr["outcome"]})

    async def event(request: web.Request) -> web.Response:
        ctx, body = await _auth(request, gate)
        jr = ctx.jr
        kind = body.get("type")
        if jr["outcome"] in OPEN and jr["path"] == "query":
            now = time.time()
            if kind == "deactivated":
                await db.update_join_request(jr["id"], grace_until=now + gate.config.grace_seconds)
                log.info("jr %s: app minimised, grace period started", jr["id"])
            elif kind == "activated":
                await db.update_join_request(jr["id"], grace_until=None, last_heartbeat=now)
                log.info("jr %s: app back in front", jr["id"])
            elif kind == "heartbeat":
                await db.update_join_request(jr["id"], last_heartbeat=now)
        return web.json_response({"open": jr["outcome"] in OPEN})

    async def index(request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEBAPP_DIR / "index.html")

    @web.middleware
    async def security_headers(request: web.Request, handler):
        response = await handler(request)
        if request.path.startswith("/app"):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.middlewares.append(security_headers)
    app.router.add_post("/api/challenge", challenge)
    app.router.add_post("/api/answer", answer)
    app.router.add_post("/api/manual", manual)
    app.router.add_post("/api/event", event)
    app.router.add_get("/app/", index)
    app.router.add_static("/app/static/", WEBAPP_DIR / "static")
