import aiohttp

from .config import Settings


class TelegramService:
    def __init__(self, settings: Settings):
        self.bot_token = settings.bot_token
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"

    async def start(self):
        return True

    async def stop(self):
        return True

    async def delete_webhook(self):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.api_base}/deleteWebhook",
                json={"drop_pending_updates": False},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram deleteWebhook failed: {data}")
                return data

    async def send_text(self, chat_id: int, text: str):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.api_base}/sendMessage",
                json={"chat_id": int(chat_id), "text": text},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram sendMessage failed: {data}")
