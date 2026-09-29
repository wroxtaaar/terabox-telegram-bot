import logging
import re
from fastapi import FastAPI, HTTPException, Request
from .config import load_settings
from .telegram import TelegramService
from .terabox import is_terabox_url, resolve_terabox_url

log=logging.getLogger(__name__)

def create_app():
    s=load_settings()
    tg=TelegramService(s)
    app=FastAPI(title="TeraBox Telegram Bot")

    @app.on_event("startup")
    async def startup():
        await tg.start()
        if s.public_base_url:
            await tg.set_webhook(s.public_base_url,s.webhook_secret)
            log.info("Telegram webhook registered")
        log.info("Telegram MTProto client started")

    @app.on_event("shutdown")
    async def shutdown():
        await tg.stop()

    @app.get("/health")
    async def health():
        return {"status":"ok","telegram":tg.client.is_connected()}

    @app.post("/webhook/{secret}")
    async def webhook(secret:str,request:Request):
        if s.webhook_secret and secret!=s.webhook_secret:
            raise HTTPException(404)
        await handle_update(await request.json(),tg)
        return {"ok":True}

    return app

async def handle_update(update,tg):
    message=update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id=(message.get("chat") or {}).get("id")
    text=(message.get("text") or "").strip()
    if not chat_id:
        return
    if text in {"/start","/help"}:
        await tg.send_text(chat_id,"Send me a TeraBox link and I will try to prepare it for Telegram.")
        return

    urls=re.findall(r"https?://\S+",text)
    if not urls:
        await tg.send_text(chat_id,"Please send a TeraBox URL.")
        return

    url=urls[0].rstrip(").,>")
    if not is_terabox_url(url):
        await tg.send_text(chat_id,"That does not look like a supported TeraBox URL.")
        return

    await tg.send_text(chat_id,"Resolving the TeraBox link…")
    try:
        resolved=await resolve_terabox_url(url)
        await tg.send_external_file(chat_id,resolved["direct_url"],caption=resolved.get("file_name"))
    except Exception as exc:
        log.exception("TeraBox delivery failed")
        await tg.send_text(chat_id,f"I couldn't deliver that link: {exc}")

