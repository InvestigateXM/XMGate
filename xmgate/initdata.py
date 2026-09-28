"""Server-side check of Telegram Mini App initData (design §6)."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(Exception):
    pass


def validate_init_data(raw: str, bot_token: str, max_age: int = 3600, now: float | None = None) -> dict:
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
    except ValueError as e:
        raise InitDataError("malformed initData") from e
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("missing hash")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("bad hash")
    try:
        auth_date = int(pairs["auth_date"])
    except (KeyError, ValueError) as e:
        raise InitDataError("missing auth_date") from e
    if (now or time.time()) - auth_date > max_age:
        raise InitDataError("stale initData")
    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Builds a valid initData string. Used by tests and the local preview."""
    from urllib.parse import urlencode

    data = {k: json.dumps(v) if isinstance(v, dict) else str(v) for k, v in fields.items()}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)
