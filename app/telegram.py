from telethon import TelegramClient
from telethon.sessions import StringSession
from .config import Settings

class TelegramService:
    def __init__(self, settings: Settings):
        self.client=TelegramClient(StringSession(),settings.api_id,settings.api_hash)
        self.bot_token=settings.bot_token
    async def start(self): await self.client.start(bot_token=self.bot_token)
    async def stop(self): await self.client.disconnect()
    async def send_text(self,chat_id:int,text:str): await self.client.send_message(chat_id,text)
    async def send_external_file(self,chat_id:int,url:str,caption=None):
        return await self.client.send_file(chat_id,url,caption=caption,supports_streaming=True)
