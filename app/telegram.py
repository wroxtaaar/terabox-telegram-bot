import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession
from .config import Settings

class TelegramService:
    def __init__(self, settings: Settings):
        self.client=TelegramClient(StringSession(),settings.api_id,settings.api_hash)
        self.bot_token=settings.bot_token

    async def start(self):
        await self.client.start(bot_token=self.bot_token)

    async def stop(self):
        await self.client.disconnect()

    async def send_text(self,chat_id:int,text:str):
        await self.client.send_message(chat_id,text)

    async def send_external_file(self,chat_id:int,url:str,caption=None,filename=None,size=0):
        # First let Telegram fetch the remote CDN URL directly.
        try:
            return await self.client.send_file(
                chat_id, url, caption=caption, supports_streaming=True
            )
        except Exception:
            # If Telegram cannot fetch the remote URL, stream it through
            # aiohttp and upload it to Telegram using MTProto.
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=15)
            ) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
                    allow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    total = int(size or response.content_length or 0)
                    if total <= 0:
                        raise RuntimeError(
                            "TeraBox did not provide a reliable file size for MTProto upload."
                        )
                    uploaded = await self.client.upload_file(
                        response.content,
                        file_size=total,
                        file_name=filename or "terabox-file",
                    )
                    return await self.client.send_file(
                        chat_id, uploaded, caption=caption, force_document=True
                    )

    async def set_webhook(self,public_base_url:str,secret:str):
        if not public_base_url:
            return False
        path=f"/webhook/{secret}" if secret else "/webhook/telegram"
        webhook_url=public_base_url.rstrip("/") + path
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{self.bot_token}/setWebhook",
                json={"url":webhook_url,"drop_pending_updates":False},
            ) as response:
                data=await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram setWebhook failed: {data}")
        return True
