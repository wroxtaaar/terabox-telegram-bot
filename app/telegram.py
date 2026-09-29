import aiohttp

from .config import Settings


class TelegramService:
    def __init__(self, settings: Settings):
        self.bot_token = settings.bot_token
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.ready = False

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self.api_base}/getMe") as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram getMe failed: {data}")
                self.ready = True
        # The HTTP API is stateless; no heavy client remains running on Render.

    async def stop(self):
        self.ready = False

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

    async def set_webhook(self, public_base_url: str, secret: str):
        path = f"/webhook/{secret}" if secret else "/webhook/telegram"
        webhook_url = public_base_url.rstrip("/") + path
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.api_base}/setWebhook",
                json={"url": webhook_url, "drop_pending_updates": False},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram setWebhook failed: {data}")
        return True
