import logging
import re
import uuid
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI, HTTPException, Request

from .config import load_settings
from .telegram import TelegramService

log = logging.getLogger(__name__)


def _valid_terabox_url(value: str) -> bool:
    try:
        host = (urlparse(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host == "terabox.com" or host.endswith(".terabox.com") or host in {
        "terabox.app",
        "www.terabox.app",
        "teraboxapp.com",
        "www.teraboxapp.com",
        "1024tera.com",
        "www.1024tera.com",
        "teraboxlink.com",
        "www.teraboxlink.com",
        "terasharelink.com",
        "www.terasharelink.com",
        "terasharefile.com",
        "www.terasharefile.com",
        "terafileshare.com",
        "www.terafileshare.com",
        "teraboxshare.com",
        "www.teraboxshare.com",
    }


async def _send_worker_job(settings, payload):
    worker_url = settings.worker_url.rstrip("/") + "/job"
    timeout = aiohttp.ClientTimeout(total=settings.worker_request_timeout, connect=10)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Worker-Secret": settings.worker_secret,
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(worker_url, json=payload, headers=headers) as response:
            body = await response.text()
            log.info(
                "VPS worker response: HTTP %s body=%s",
                response.status,
                body[:500],
            )
            if response.status >= 400:
                raise RuntimeError(
                    f"VPS worker HTTP {response.status}: {body[:240]}"
                )


def create_app():
    s = load_settings()
    tg = TelegramService(s)
    app = FastAPI(title="TeraBox Telegram Controller")

    @app.on_event("startup")
    async def startup():
        await tg.start()
        if not s.public_base_url:
            log.warning("PUBLIC_BASE_URL is not configured; Telegram webhook will not be registered.")
        else:
            await tg.set_webhook(s.public_base_url, s.webhook_secret)
            log.info("Telegram webhook registered on Render controller.")
        log.info(
            "Render controller started; heavy resolver/download/MTProto work is delegated to VPS=%s",
            bool(s.worker_url),
        )

    @app.on_event("shutdown")
    async def shutdown():
        await tg.stop()

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "telegram": tg.ready,
            "worker_configured": bool(s.worker_url and s.worker_secret),
        }

    @app.post("/worker-callback/{secret}")
    async def worker_callback(secret: str, request: Request):
        if not s.worker_callback_secret or secret != s.worker_callback_secret:
            raise HTTPException(404)

        data = await request.json()
        job_id = str(data.get("job_id") or "")
        status = str(data.get("status") or "")
        chat_id = data.get("chat_id")
        message = str(data.get("message") or "")

        log.info(
            "VPS callback job=%s status=%s chat_id=%s message=%s",
            job_id,
            status,
            chat_id,
            message[:500],
        )

        if status == "failed" and chat_id:
            await tg.send_text(
                int(chat_id),
                f"❌ TeraBox delivery failed: {message or 'Unknown error'}",
            )
        elif status == "completed" and chat_id:
            filename = str(data.get("file_name") or "").strip()
            await tg.send_text(
                int(chat_id),
                f"✅ Sent successfully{': ' + filename if filename else '.'}",
            )

        return {"ok": True}

    @app.post("/webhook/{secret}")
    async def webhook(secret: str, request: Request):
        if s.webhook_secret and secret != s.webhook_secret:
            raise HTTPException(404)

        update = await request.json()
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"ok": True}

        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}

        if text in {"/start", "/help"}:
            await tg.send_text(
                chat_id,
                "Send me a TeraBox link and I will resolve and send the file.",
            )
            return {"ok": True}

        urls = re.findall(r"https?://\S+", text)
        if not urls:
            await tg.send_text(chat_id, "Please send a TeraBox URL.")
            return {"ok": True}

        url = urls[0].rstrip(").,>")
        if not _valid_terabox_url(url):
            await tg.send_text(chat_id, "That does not look like a supported TeraBox URL.")
            return {"ok": True}

        if not s.worker_url or not s.worker_secret:
            log.error("VPS worker is not configured; refusing to process heavy work on Render.")
            await tg.send_text(
                chat_id,
                "⚠️ The download worker is not configured yet. Please try again later.",
            )
            return {"ok": True}

        job_id = uuid.uuid4().hex
        await tg.send_text(
            chat_id,
            f"⏳ Queued your TeraBox link. Job: {job_id[:8]}",
        )

        callback_url = (
            s.public_base_url.rstrip("/")
            + "/worker-callback/"
            + s.worker_callback_secret
        )

        payload = {
            "job_id": job_id,
            "chat_id": int(chat_id),
            "url": url,
            "callback_url": callback_url,
        }

        try:
            await _send_worker_job(s, payload)
        except Exception as exc:
            log.exception("Could not enqueue VPS worker job %s", job_id)
            await tg.send_text(
                chat_id,
                f"❌ Could not queue the download: {exc}",
            )
            return {"ok": True}

        return {"ok": True, "job_id": job_id}

    return app
