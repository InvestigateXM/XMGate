"""HTTP API the captcha Mini App talks to. Every call carries initData (design §6)."""

import hashlib
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
    def __init__(self, user_id: int, username: str | None, jr):
        self.user_id = user_id
        self.username = username
        self.jr = jr  # None when the app was opened from "Verify me" in the DM menu


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
    valid, jr_id = gate.parse_token(str(body.get("r", "")), user_id)
    if not valid:
        raise web.HTTPForbidden(text="this link is not for you")
    jr = None
    if jr_id is not None:
        jr = await gate.db.get_join_request(jr_id)
        if jr is None:
            raise web.HTTPNotFound(text="request not found")
    return Ctx(user_id, init["user"].get("username"), jr), body


def _asset_version() -> str:
    digest = hashlib.sha256()
    for path in sorted((WEBAPP_DIR / "static").iterdir()):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _base(jr) -> dict:
    if jr is None:
        return {"chat_title": None, "chat_link": None, "self": True}
    link = f"https://t.me/{jr['chat_username']}" if jr["chat_username"] else None
    return {"chat_title": jr["chat_title"], "chat_link": link, "self": False}


def setup_api(app: web.Application, gate: Gate) -> None:
    db = gate.db

    async def challenge(request: web.Request) -> web.Response:
        ctx, _ = await _auth(request, gate)
        jr = ctx.jr
        base = _base(jr)
        user = await db.get_user(ctx.user_id)
        verified = bool(user and user["verified_at"])

        if jr is None:
            # "Verify me" from the DM menu. Opening it turns processing back on for an opted-out user.
            if await db.remove_opt_out(gate.optout_marker(ctx.user_id)):
                log.info("an opted-out user started verification, opt-out removed")
            if verified:
                return web.json_response({**base, "status": "already_verified"})
            await db.upsert_user(ctx.user_id, ctx.username)
        else:
            if jr["outcome"] in ("approved", "admin_approved"):
                return web.json_response({**base, "status": "approved"})
            if verified:
                if jr["outcome"] in OPEN:
                    await gate.resolve(jr, "approve")
                    return web.json_response({**base, "status": "approved"})
                return web.json_response({**base, "status": "verified_late"})
            if jr["outcome"] == "queued":
                return web.json_response({**base, "status": "queued"})

        misses = await db.recent_misses(ctx.user_id, time.time() - captcha.MISS_WINDOW)
        wait = captcha.wait_seconds(misses)
        suggest_manual = jr is not None and len(misses) >= captcha.SUGGEST_MANUAL_AFTER
        if wait:
            return web.json_response({**base, "status": "wait", "wait": wait, "suggest_manual": suggest_manual})

        stored, public = captcha.new_challenge()
        attempt_id = secrets.token_urlsafe(12)
        await db.add_attempt(attempt_id, ctx.user_id, jr["id"] if jr else None, stored)
        return web.json_response(
            {
                **base,
                "status": "challenge",
                "attempt": attempt_id,
                "tiles": public,
                "columns": captcha.COLUMNS,
                "misses": len(misses),
                "suggest_manual": suggest_manual,
                "request_open": jr is None or jr["outcome"] in OPEN,
            }
        )

    async def answer(request: web.Request) -> web.Response:
        ctx, body = await _auth(request, gate)
        jr_id = ctx.jr["id"] if ctx.jr else None
        attempt = await db.get_attempt(str(body.get("attempt", "")))
        if attempt is None or attempt["tg_user_id"] != ctx.user_id or attempt["join_request_id"] != jr_id:
            raise web.HTTPNotFound(text="unknown attempt")
        picked = [str(p) for p in body.get("picked", [])][:captcha.TILE_COUNT]
        correct = captcha.is_correct(attempt["tiles"], picked)
        solve_ms = body.get("solve_ms")
        if not await db.answer_attempt(
            attempt["id"], correct, int(solve_ms) if isinstance(solve_ms, (int, float)) else None, bool(body.get("had_touch"))
        ):
            raise web.HTTPConflict(text="attempt already answered")
        base = _base(ctx.jr)
        if not correct:
            log.info("jr %s: wrong answer from user %s", jr_id, ctx.user_id)
            return web.json_response({**base, "status": "wrong"})
        approved = await gate.on_solved(ctx.user_id, ctx.jr["chat_id"] if ctx.jr else None)
        if ctx.jr is None:
            return web.json_response({**base, "status": "verified_self", "approved": approved})
        jr = await db.get_join_request(jr_id)
        status = "approved" if jr["outcome"] == "approved" else "verified_late"
        return web.json_response({**base, "status": status})

    async def manual(request: web.Request) -> web.Response:
        ctx, _ = await _auth(request, gate)
        if ctx.jr is None:
            raise web.HTTPBadRequest(text="no join request to hand to the admins")
        if ctx.jr["outcome"] in OPEN:
            await gate.resolve(ctx.jr, "queue")
        jr = await db.get_join_request(ctx.jr["id"])
        return web.json_response({"status": "queued" if jr["outcome"] == "queued" else jr["outcome"]})

    async def event(request: web.Request) -> web.Response:
        ctx, body = await _auth(request, gate)
        jr = ctx.jr
        if jr is None:
            return web.json_response({"open": False})
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

    # Telegram's in-app browser caches the Mini App's script and stylesheet hard, so a phone
    # could run an old app.js against a newer server. Versioned URLs force a fresh copy.
    version = _asset_version()
    page = (WEBAPP_DIR / "index.html").read_text()
    for asset in ("static/app.js", "static/style.css"):
        page = page.replace(f'"{asset}"', f'"{asset}?v={version}"')

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

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
