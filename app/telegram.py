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

    async def send_external_file(self,chat_id:int,url:str,caption=None):
        return await self.client.send_file(chat_id,url,caption=caption,supports_streaming=True)

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
