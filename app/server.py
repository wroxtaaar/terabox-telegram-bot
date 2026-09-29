import logging

from fastapi import FastAPI

from .config import load_settings
from .telegram import TelegramService

log = logging.getLogger(__name__)


def create_app():
    settings = load_settings()
    telegram = TelegramService(settings)
    app = FastAPI(title="TeraBox Render Retirement Stub")

    @app.on_event("startup")
    async def startup():
        # Remove the old Bot API webhook so the retired Render service cannot
        # compete with the Oracle MTProto bot for Telegram traffic.
        try:
            await telegram.delete_webhook()
            log.info("Retired Render controller: Telegram webhook deleted.")
        except Exception:
            log.exception("Could not delete the old Telegram webhook.")

    @app.on_event("shutdown")
    async def shutdown():
        await telegram.stop()

    @app.get("/health")
    async def health():
        return {
            "status": "retired",
            "message": "Telegram bot now runs entirely on the Oracle VPS.",
        }

    @app.api_route("/webhook/{secret}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def retired_webhook(secret: str):
        return {
            "ok": False,
            "status": "retired",
            "message": "Telegram bot moved to Oracle VPS.",
        }

    return app
