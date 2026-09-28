import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    base_url: str  # public HTTPS origin, e.g. https://gate.example.com
    webhook_secret: str
    mode: str  # "webhook" or "polling"
    grace_seconds: int
    db_path: str
    port: int

    @property
    def webhook_url(self) -> str:
        return f"{self.base_url}/webhook"

    @property
    def app_url(self) -> str:
        return f"{self.base_url}/app/"


def load() -> Config:
    def need(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise SystemExit(f"Missing environment variable {name} (see .env.example)")
        return value

    mode = os.environ.get("MODE", "webhook").strip().lower()
    return Config(
        bot_token=need("BOT_TOKEN"),
        base_url=need("BASE_URL").rstrip("/"),
        webhook_secret=need("WEBHOOK_SECRET") if mode == "webhook" else os.environ.get("WEBHOOK_SECRET", ""),
        mode=mode,
        grace_seconds=int(os.environ.get("GRACE_SECONDS", "300")),
        db_path=os.environ.get("DB_PATH", "data/xmgate.sqlite3"),
        port=int(os.environ.get("PORT", "8080")),
    )
