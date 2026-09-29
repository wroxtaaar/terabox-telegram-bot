import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    webhook_secret: str
    public_base_url: str
    port: int
    worker_url: str
    worker_secret: str
    worker_callback_secret: str
    worker_request_timeout: int


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    missing = [k for k, v in (("BOT_TOKEN", bot_token),) if not v]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

    return Settings(
        bot_token=bot_token,
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        port=int(os.getenv("PORT", "10000")),
        worker_url=os.getenv("VPS_WORKER_URL", "").strip().rstrip("/"),
        worker_secret=os.getenv("VPS_WORKER_SECRET", "").strip(),
        worker_callback_secret=os.getenv("VPS_CALLBACK_SECRET", "").strip(),
        worker_request_timeout=int(os.getenv("VPS_WORKER_REQUEST_TIMEOUT", "20")),
    )
