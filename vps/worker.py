from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import SQLiteSession

from .browser_resolver import TeraBoxBrowserResolver
from .splitter import MAX_TELEGRAM_FILE_SIZE, split_binary_file, split_video
from .stream_downloader import download_m3u8_stream


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("terabox-vps-worker")


TERABOX_HOSTS = {
    "terabox.com",
    "www.terabox.com",
    "1024terabox.com",
    "www.1024terabox.com",
    "teraboxapp.com",
    "www.teraboxapp.com",
    "terabox.app",
    "www.terabox.app",
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
    "nephobox.com",
    "www.nephobox.com",
    "mirrobox.com",
    "www.mirrobox.com",
    "mirrorbox.com",
    "www.mirrorbox.com",
    "momerybox.com",
    "www.momerybox.com",
    "tibibox.com",
    "www.tibibox.com",
    "gibibox.com",
    "www.gibibox.com",
    "pebibox.com",
    "www.pebibox.com",
    "4funbox.com",
    "www.4funbox.com",
    "dubox.com",
    "www.dubox.com",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".ts",
    ".flv",
}

MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "100"))
MAX_FILES_PER_LINK = int(os.getenv("MAX_FILES_PER_LINK", "25"))
MAX_SOURCE_FILE_SIZE_BYTES = int(
    os.getenv("MAX_SOURCE_FILE_SIZE_BYTES", str(2 * 1024 * 1024 * 1024))
)
MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(10 * 1024 * 1024 * 1024))
)
MAX_ZIP_SIZE_BYTES = int(
    os.getenv("MAX_ZIP_SIZE_BYTES", str(250 * 1024 * 1024))
)
MAX_MT_PROTO_FILE_SIZE = int(
    os.getenv("MAX_MT_PROTO_FILE_SIZE", str(2 * 1024 * 1024 * 1024))
)

DATA_DIR = Path(os.getenv("DATA_DIR", "/worker/data"))
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "/tmp/terabox-downloads"))
UNPACKED_DIR = Path(os.getenv("UNPACKED_DIR", "/tmp/terabox-unpacked"))
JOBS_FILE = DATA_DIR / "jobs.json"
DUPLICATE_LINKS_FILE = DATA_DIR / "recent_links.json"
DUPLICATE_LINK_COOLDOWN_SECONDS = 10 * 60


@dataclass(slots=True)
class QueueTask:
    task_id: str
    chat_id: int
    url: str
    file_names: list[str] | None = None
    size_bytes: int | None = None
    # Queue size is the actual total source size reported by TeraBox.
    cancel_requested: bool = False
    retry_count: int = 0
    queued_at: float = field(default_factory=time.time)
    job_id: str | None = None


def format_bytes(value: int | float) -> str:
    value = float(value or 0)
    if value <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    index = min(len(units) - 1, int(math.log(value, 1024)) if value >= 1024 else 0)
    return f"{value / (1024 ** index):.2f}".rstrip("0").rstrip(".") + f" {units[index]}"


def safe_filename(value: str) -> str:
    value = str(value or "").replace("\\", "/")
    value = Path(value).name.strip()
    value = re.sub(r"[<>:\"/|?*\x00-\x1f]", "_", value)
    value = value.strip(" .")
    return value[:240] or "terabox-file"


def extract_url_from_text(text: str) -> str | None:
    direct = re.search(r"https?://\S+", text or "", re.IGNORECASE)
    if direct:
        return direct.group(0).rstrip(").,]>")
    bare = re.search(r"(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}/\S+", text or "")
    if bare:
        return "https://" + bare.group(0).rstrip(").,]>")
    return None


def is_terabox_url(value: str) -> bool:
    try:
        host = (urlparse(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return (
        host in TERABOX_HOSTS
        or host.endswith(".terabox.com")
        or any(
            host == item or host.endswith("." + item)
            for item in ("terashare.com", "dubox.com", "nephobox.com")
        )
    )


def normalize_link(raw_link: str) -> str:
    extracted = extract_url_from_text((raw_link or "").strip()) or (raw_link or "").strip()
    if not extracted:
        return ""
    try:
        parsed = urlparse(extracted)
        return parsed._replace(fragment="").geturl().rstrip("/")
    except Exception:
        return re.sub(r"\s+", "", extracted).rstrip("/")


def get_directory_size(directory: Path) -> int:
    if not directory.exists():
        return 0
    total = 0
    for entry in directory.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def detect_extension_from_buffer(header: bytes) -> str | None:
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".mp4"
    if len(header) >= 4 and header[:4] == bytes.fromhex("1a45dfa3"):
        return ".mkv"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return ".avi"
    if header[:3] == b"ID3" or (
        len(header) >= 2 and header[0] == 0xFF and header[1] in (0xFB, 0xF3, 0xF2)
    ):
        return ".mp3"
    if header[:4] == bytes.fromhex("504b0304"):
        return ".zip"
    if header[:6] == b"Rar!\x1a\x07":
        return ".rar"
    if header[:6] == bytes.fromhex("377abcaf271c"):
        return ".7z"
    if header[:4] == b"%PDF":
        return ".pdf"
    if header[:3] == bytes.fromhex("ffd8ff"):
        return ".jpg"
    if header[:4] == bytes.fromhex("89504e47"):
        return ".png"
    return None


class Worker:
    def __init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        try:
            self.api_id = int(os.getenv("API_ID", "0"))
        except ValueError as exc:
            raise RuntimeError("API_ID must be numeric.") from exc
        self.api_hash = os.getenv("API_HASH", "").strip()

        if not self.bot_token or not self.api_id or not self.api_hash:
            raise RuntimeError("BOT_TOKEN, API_ID and API_HASH are required.")

        for directory in (DATA_DIR, DOWNLOADS_DIR, UNPACKED_DIR, DOWNLOADS_DIR / "active"):
            directory.mkdir(parents=True, exist_ok=True)

        self.session_dir = Path(os.getenv("TELETHON_SESSION_DIR", "/worker/data"))
        self.session_dir.mkdir(parents=True, exist_ok=True)
        token_fingerprint = hashlib.sha256(
            self.bot_token.encode("utf-8")
        ).hexdigest()[:16]
        self.session_path = str(
            self.session_dir / f"terabox_bot_{token_fingerprint}"
        )
        self.telegram = TelegramClient(
            SQLiteSession(self.session_path),
            self.api_id,
            self.api_hash,
        )

        # Inspect metadata concurrently so the ready-download queue fills
        # quickly while the single active download runs independently.
        self.resolver = TeraBoxBrowserResolver(max_concurrent=2)

        self.size_inspection_queue: list[QueueTask] = []
        # Min-heap ordered by file size, then enqueue time, then task id.
        # Unknown-size tasks are kept in the inspection queue until sized.
        self.download_queue: list[tuple[int, float, str, QueueTask]] = []
        self.active_task: QueueTask | None = None
        self.inspecting_task: QueueTask | None = None
        self.size_event = asyncio.Event()
        self.download_event = asyncio.Event()
        self.stop_event = asyncio.Event()

        self.size_task: asyncio.Task | None = None
        self.download_task: asyncio.Task | None = None
        self.telegram_task: asyncio.Task | None = None
        self.http_session: aiohttp.ClientSession | None = None

        self.jobs: list[dict] = self._load_jobs()
        self.recent_links: dict[str, float] = self._load_recent_links()
        self.link_counters: dict[str, tuple[str, int]] = {}

        log.info(
            "Telethon session selected by BOT_TOKEN fingerprint=%s",
            token_fingerprint,
        )
        try:
            import cryptg  # noqa: F401
            log.info("cryptg acceleration is available for Telethon.")
        except ImportError:
            log.warning("cryptg is not installed; Telethon encryption will use Python fallbacks.")

    # ---------- persistence / queue helpers ----------

    @staticmethod
    def _load_jobs() -> list[dict]:
        try:
            if JOBS_FILE.exists():
                value = json.loads(JOBS_FILE.read_text("utf-8"))
                if isinstance(value, list):
                    return value[:50]
        except Exception:
            log.exception("Failed to load jobs from disk")
        return []

    @staticmethod
    def _load_recent_links() -> dict[str, float]:
        try:
            if DUPLICATE_LINKS_FILE.exists():
                value = json.loads(DUPLICATE_LINKS_FILE.read_text("utf-8"))
                if isinstance(value, dict):
                    now = time.time()
                    return {
                        str(url): float(timestamp)
                        for url, timestamp in value.items()
                        if isinstance(timestamp, (int, float))
                        and now - float(timestamp) < DUPLICATE_LINK_COOLDOWN_SECONDS
                    }
        except Exception:
            log.exception("Failed to load recent TeraBox links")
        return {}

    def _save_recent_links(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            now = time.time()
            self.recent_links = {
                url: timestamp
                for url, timestamp in self.recent_links.items()
                if now - timestamp < DUPLICATE_LINK_COOLDOWN_SECONDS
            }
            tmp = DUPLICATE_LINKS_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.recent_links, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(DUPLICATE_LINKS_FILE)
        except Exception:
            log.exception("Failed to save recent TeraBox links")

    def _check_duplicate_link(self, url: str) -> int | None:
        normalized = normalize_link(url)
        if not normalized:
            return None
        now = time.time()
        previous = self.recent_links.get(normalized)
        if previous is not None:
            elapsed = now - previous
            if elapsed < DUPLICATE_LINK_COOLDOWN_SECONDS:
                return max(1, int(DUPLICATE_LINK_COOLDOWN_SECONDS - elapsed))
        self.recent_links[normalized] = now
        self._save_recent_links()
        return None

    def _save_jobs(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = JOBS_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.jobs[:50], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(JOBS_FILE)
        except Exception:
            log.exception("Failed to save jobs")

    def _link_counter(self, url: str) -> int:
        normalized = normalize_link(url)
        day = datetime.now().strftime("%Y-%m-%d")
        existing = self.link_counters.get(normalized)
        if existing and existing[0] == day:
            return existing[1]
        number = len(
            [key for key, value in self.link_counters.items() if value[0] == day]
        ) + 1
        self.link_counters[normalized] = (day, number)
        return number

    def _link_counter_text(self, url: str) -> str:
        return f"#{self._link_counter(url)}"

    def _known_downloaded_size(self, url: str) -> int | None:
        normalized = normalize_link(url)
        for job in self.jobs:
            if (
                job.get("status") == "completed"
                and normalize_link(str(job.get("url") or "")) == normalized
                and job.get("files")
            ):
                return sum(
                    int(item.get("sizeBytes") or 0)
                    for item in job.get("files", [])
                    if isinstance(item, dict)
                )
        return None

    async def _resolve_missing_file_sizes(self, files: list[dict]) -> None:
        """Fill missing TeraBox sizes from resolved download URLs without downloading data."""
        if not self.http_session:
            return

        timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=8)
        for item in files:
            try:
                current_size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                current_size = 0
            if current_size > 0:
                continue

            url = str(item.get("direct_url") or "").strip()
            if not url:
                continue

            try:
                async with self.http_session.head(
                    url,
                    allow_redirects=True,
                    timeout=timeout,
                ) as response:
                    content_length = int(response.headers.get("Content-Length") or 0)
                    if content_length > 0:
                        item["size"] = content_length
                        continue

                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.search(r"/(\d+)$", content_range)
                    if match:
                        item["size"] = int(match.group(1))
            except Exception as exc:
                log.debug(
                    "Could not resolve size for %s: %s",
                    item.get("file_name") or "unknown file",
                    self._compact_error(exc),
                )

    def _queued_count(self) -> int:
        return len(self.size_inspection_queue) + len(self.download_queue)

    def _queue_position(self) -> int:
        return self._queued_count() + (1 if self.active_task else 0)

    @staticmethod
    def _sort_key(task: QueueTask) -> tuple[int, float, str]:
        return (
            task.size_bytes if task.size_bytes is not None else 2**63 - 1,
            task.queued_at,
            task.task_id,
        )

    @staticmethod
    def _heap_item(task: QueueTask) -> tuple[int, float, str, QueueTask]:
        size = task.size_bytes if task.size_bytes is not None else 2**63 - 1
        return (size, task.queued_at, task.task_id, task)

    def _push_download_task(self, task: QueueTask) -> None:
        heapq.heappush(self.download_queue, self._heap_item(task))

    def _next_download_task(self) -> QueueTask | None:
        if not self.download_queue:
            return None
        return heapq.heappop(self.download_queue)[3]

    def _find_task(self, task_id: str) -> QueueTask | None:
        if self.active_task and self.active_task.task_id == task_id:
            return self.active_task
        for task in self.size_inspection_queue:
            if task.task_id == task_id:
                return task
        for _, _, _, task in self.download_queue:
            if task.task_id == task_id:
                return task
        return None

    def _remove_task(self, task: QueueTask) -> None:
        try:
            self.size_inspection_queue.remove(task)
            return
        except ValueError:
            pass

        for index, (_, _, _, queued_task) in enumerate(self.download_queue):
            if queued_task is task:
                self.download_queue.pop(index)
                heapq.heapify(self.download_queue)
                return

    async def _request_cancel(self, task: QueueTask) -> bool:
        if task.cancel_requested:
            return True
        task.cancel_requested = True
        self._remove_task(task)
        return True

    # ---------- lifecycle ----------

    async def start(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        UNPACKED_DIR.mkdir(parents=True, exist_ok=True)

        log.info("Starting Chromium resolver...")
        await self.resolver.start()

        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=120),
            connector=aiohttp.TCPConnector(
                limit=16,
                limit_per_host=16,
                ttl_dns_cache=300,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            ),
        )

        log.info("Removing any legacy Telegram Bot API webhook...")
        await self._delete_bot_api_webhook()
        await self._set_bot_commands()

        self.stop_event.clear()
        self.size_task = asyncio.create_task(self._size_inspection_loop())
        self.download_task = asyncio.create_task(self._download_queue_loop())
        self.telegram_task = asyncio.create_task(self._connect_telegram_with_retry())

        log.info(
            "VPS worker started; ready downloads begin immediately while remaining "
            "queue items continue metadata inspection in the background."
        )

    async def stop(self):
        self.stop_event.set()
        self.size_event.set()
        self.download_event.set()

        for task in (
            self.size_task,
            self.download_task,
            self.telegram_task,
        ):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self.size_task = None
        self.download_task = None
        self.telegram_task = None

        if self.telegram.is_connected():
            await self.telegram.disconnect()

        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        self.http_session = None

        await self.resolver.stop()

    async def _delete_bot_api_webhook(self):
        timeout = aiohttp.ClientTimeout(total=15, connect=8)
        api_url = f"https://api.telegram.org/bot{self.bot_token}/deleteWebhook"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                api_url,
                json={"drop_pending_updates": False},
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400 or not data.get("ok"):
                    raise RuntimeError(f"Telegram deleteWebhook failed: {data}")
        log.info("Telegram Bot API webhook removed; Oracle will receive updates via MTProto.")

    async def _set_bot_commands(self):
        commands = [
            {"command": "start", "description": "Start the bot"},
            {"command": "queue", "description": "View the current download queue"},
            {"command": "space", "description": "Check server storage"},
            {"command": "status", "description": "Check bot status"},
            {"command": "help", "description": "Show help"},
            {"command": "cancel", "description": "Cancel an active or queued download"},
        ]
        timeout = aiohttp.ClientTimeout(total=15, connect=8)
        api_url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, json={"commands": commands}) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400 or not data.get("ok"):
                        log.warning("Telegram setMyCommands failed: %s", data)
        except Exception:
            log.exception("Could not register Telegram commands")

    async def _connect_telegram_with_retry(self):
        registered = False
        while not self.stop_event.is_set():
            try:
                log.info("Starting Telegram MTProto bot client on VPS...")
                await self.telegram.start(bot_token=self.bot_token)

                if not registered:
                    self.telegram.add_event_handler(
                        self._on_message,
                        events.NewMessage(incoming=True),
                    )
                    self.telegram.add_event_handler(
                        self._on_callback,
                        events.CallbackQuery(),
                    )
                    registered = True

                me = await self.telegram.get_me()
                log.info(
                    "Telegram bot connected: username=@%s id=%s",
                    getattr(me, "username", None),
                    getattr(me, "id", None),
                )
                log.info("VPS worker ready; Telegram intake is running directly on Oracle.")
                return
            except FloodWaitError as exc:
                wait_seconds = max(1, int(exc.seconds))
                log.warning(
                    "Telegram requested FloodWait=%ss during bot authorization. "
                    "Keeping the worker alive and retrying after the wait.",
                    wait_seconds,
                )
                if self.telegram.is_connected():
                    await self.telegram.disconnect()
                await asyncio.sleep(wait_seconds)
            except Exception:
                log.exception("Telegram MTProto connection failed; retrying in 30 seconds.")
                if self.telegram.is_connected():
                    await self.telegram.disconnect()
                await asyncio.sleep(30)

    # ---------- telegram input ----------

    async def _on_message(self, event):
        if not self.telegram.is_connected():
            return

        text = (event.raw_text or "").strip()
        chat_id = event.chat_id
        if not chat_id:
            return
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return

        command_token = text.split(None, 1)[0] if text else ""
        command = command_token.split("@", 1)[0].lower()

        if command == "/start":
            await event.reply(
                "👋 TeraBox Downloader Bot Active\n\n"
                "Send me any supported TeraBox share link and I will download "
                "the files and deliver them here.\n\n"
                "Commands:\n"
                "/queue — current download queue\n"
                "/space — server storage\n"
                "/status — bot status\n"
                "/cancel [job/task id] — cancel a download\n"
                "/help — help and usage"
            )
            return

        if command == "/help":
            await event.reply(
                "📖 Help\n\n"
                "1. Paste a TeraBox share link.\n"
                "2. The bot inspects file names and sizes in the background.\n"
                "3. Downloading starts as soon as a task is sized; ready tasks are processed smallest-first.\n"
                "4. ZIP archives are unpacked automatically.\n"
                "5. Videos use streamable Telegram delivery when possible.\n"
                "6. Large files use direct MTProto upload first; if that fails, the bot splits them into parts.\n\n"
                "Commands:\n"
                "/queue — inspect waiting and active jobs\n"
                "/space — storage and temporary disk usage\n"
                "/status — worker and queue status\n"
                "/cancel — cancel your active/queued job"
            )
            return

        if command == "/status":
            await self._send_status_command(chat_id)
            return

        if command == "/queue":
            await self._send_queue_command(chat_id)
            return

        if command in {"/space", "/disk"}:
            await self._send_space_command(chat_id)
            return

        if command == "/cancel":
            argument = ""
            if len(text.split(None, 1)) > 1:
                argument = text.split(None, 1)[1].strip().split()[0]
            await self._cancel_from_command(chat_id, argument)
            return

        url = extract_url_from_text(text)
        if not url or not is_terabox_url(url):
            return

        if self._queue_position() >= MAX_QUEUE_SIZE:
            await event.reply("⚠️ The download queue is full. Please try again later.")
            return

        remaining_cooldown = self._check_duplicate_link(url)
        if remaining_cooldown is not None:
            minutes = max(1, math.ceil(remaining_cooldown / 60))
            await event.reply(
                f"⏳ This TeraBox link was already submitted recently. "
                f"Please wait about {minutes} minute{'s' if minutes != 1 else ''} before sending it again."
            )
            log.info(
                "telegram duplicate link rejected chat_id=%s cooldown_remaining=%ss url=%s",
                chat_id,
                remaining_cooldown,
                url,
            )
            return

        task = QueueTask(
            task_id=uuid.uuid4().hex,
            chat_id=chat_id,
            url=url,
        )
        self.size_inspection_queue.append(task)
        self.size_event.set()

        position = self._queue_position()
        await event.reply(
            f"📋 Queued your TeraBox link.\n"
            f"Job/task: {task.task_id[:8]}\n"
            f"Queue position: {position}\n\n"
            f"🔎 First checking file names and sizes…",
            buttons=[[Button.inline("🛑 Cancel", data=f"cancel:{task.task_id}".encode())]],
        )

        log.info(
            "telegram message accepted task=%s chat_id=%s queue_size_before=%s url=%s",
            task.task_id,
            chat_id,
            position - 1,
            url,
        )

    async def _on_callback(self, event):
        try:
            raw = event.data or b""
            data = raw.decode("utf-8", "replace")
        except Exception:
            data = ""

        if not data.startswith("cancel:"):
            return

        task_id = data.split(":", 1)[1].strip()
        task = self._find_task(task_id)
        if not task:
            await event.answer("This download has already finished.", alert=True)
            return

        if task.cancel_requested:
            await event.answer("Cancellation already requested.")
            return

        await self._request_cancel(task)
        await event.answer("Cancellation requested.")
        try:
            if event.chat_id:
                await self.telegram.send_message(
                    event.chat_id,
                    f"🛑 Cancellation requested for {task_id[:8]}.",
                )
        except Exception:
            pass

    async def _cancel_from_command(self, chat_id: int, argument: str):
        task: QueueTask | None = None

        if argument:
            task = self._find_task(argument)
        else:
            if self.active_task and self.active_task.chat_id == chat_id:
                task = self.active_task
            else:
                candidates = [
                    item
                    for item in (
                        self.size_inspection_queue
                        + [queued_task for _, _, _, queued_task in self.download_queue]
                    )
                    if item.chat_id == chat_id
                ]
                if candidates:
                    task = min(candidates, key=lambda item: item.queued_at)

        if not task or task.chat_id != chat_id:
            await self.telegram.send_message(
                chat_id,
                "ℹ️ No matching active or queued download was found.",
            )
            return

        await self._request_cancel(task)
        await self.telegram.send_message(
            chat_id,
            f"🛑 Cancellation requested for {task.task_id[:8]}.",
        )

    # ---------- queue commands ----------

    async def _send_status_command(self, chat_id: int):
        active = self.active_task
        active_name = (
            ", ".join(active.file_names)
            if active and active.file_names
            else "Nothing"
        )
        completed = sum(1 for job in self.jobs if job.get("status") == "completed")
        failed = sum(1 for job in self.jobs if job.get("status") == "failed")

        text = (
            "⚡ Bot Status\n\n"
            f"Telegram: {'Online' if self.telegram.is_connected() else 'Connecting'}\n"
            f"Browser: {'Ready' if self.resolver.browser else 'Starting'}\n"
            f"🔄 Active: {active_name}\n"
            f"🔎 Finding sizes: {len(self.size_inspection_queue) + (1 if self.inspecting_task else 0)}\n"
            f"📦 Ready to download: {len(self.download_queue)}\n"
            f"📁 Processed jobs: {len(self.jobs)}\n"
            f"✅ Completed: {completed}\n"
            f"❌ Failed: {failed}"
        )
        await self.telegram.send_message(chat_id, text)

    @staticmethod
    def _queue_filename(name: str) -> str:
        name = str(name or "").strip()
        if not name:
            return "Unknown"
        # Keep queue entries compact enough to stay on one Telegram line.
        return name if len(name) <= 12 else name[:12] + "..."

    def _queue_names(self, task: QueueTask) -> str:
        if task.file_names is None:
            return "Checking..."
        if not task.file_names:
            return "Unknown"

        first = self._queue_filename(task.file_names[0])
        extra = len(task.file_names) - 1
        return f"{first} +{extra} more" if extra else first

    def _queue_task_line(self, index: int, task: QueueTask) -> str:
        name_label = self._queue_names(task)
        if task.size_bytes and task.size_bytes > 0:
            size = format_bytes(task.size_bytes)
        else:
            size = "Resolving..."
        return f"{index}. {size} — {name_label} [{task.task_id[:8]}]"

    async def _send_queue_command(self, chat_id: int):
        active = self.active_task
        active_line = "🔄 Now processing: Nothing"
        if active:
            names = self._queue_names(active)
            size = format_bytes(active.size_bytes or 0)
            active_line = (
                f"🔄 Now processing: {size} — {names}"
                + f" [{active.task_id[:8]}]"
            )

        inspection_count = len(self.size_inspection_queue) + (1 if self.inspecting_task else 0)
        ready = sorted((queued_task for _, _, _, queued_task in self.download_queue), key=self._sort_key)
        queue_lines = (
            "\n".join(self._queue_task_line(i + 1, task) for i, task in enumerate(ready))
            if ready
            else "Empty"
        )

        text = (
            "📋 Download Queue\n\n"
            + active_line
            + "\n\n"
            + (f"🔎 Resolving actual file sizes ({inspection_count}): Please wait…\n\n" if inspection_count else "")
            + f"⏳ Download queue ({len(ready)} waiting, smallest first):\n{queue_lines}"
        )
        await self.telegram.send_message(chat_id, text[:3900])

    async def _send_space_command(self, chat_id: int):
        try:
            stat = os.statvfs(DATA_DIR)
            total_bytes = int(stat.f_blocks * stat.f_frsize)
            free_bytes = int(stat.f_bavail * stat.f_frsize)
            used_bytes = total_bytes - int(stat.f_bfree * stat.f_frsize)
        except OSError:
            total_bytes = free_bytes = used_bytes = 0

        used_percent = round(used_bytes * 100 / total_bytes) if total_bytes else 0
        bot_files = get_directory_size(DATA_DIR)
        temporary_downloads = get_directory_size(DOWNLOADS_DIR)
        unpacked_files = get_directory_size(UNPACKED_DIR)

        text = (
            "💾 Bot Storage\n\n"
            f"📦 Bot files: {format_bytes(bot_files)}\n"
            f"⬇️ Temporary downloads: {format_bytes(temporary_downloads)}\n"
            f"🗜️ Unpacked files: {format_bytes(unpacked_files)}\n"
            "🧹 Cleanup: after every job and on startup\n"
            f"⏳ Queue: {'1 active' if self.active_task else 'No active job'}, "
            f"{self._queued_count()} waiting\n\n"
            "🖥️ Container filesystem reference\n"
            f"• Used: {format_bytes(used_bytes)} ({used_percent}%)\n"
            f"• Free: {format_bytes(free_bytes)}\n"
            f"• Total: {format_bytes(total_bytes)}\n"
            f"• Location: {DATA_DIR}"
        )
        await self.telegram.send_message(chat_id, text)

    # ---------- size inspection ----------

    async def _inspect_one_task(self, task: QueueTask) -> None:
        """Inspect one link and promote it immediately when metadata is ready."""
        self.inspecting_task = task
        try:
            log.info("task=%s inspecting TeraBox metadata", task.task_id)
            metadata = await self.resolver.resolve(
                task.url,
                allow_native_download=False,
            )
            files = [
                item for item in (metadata.get("files") or [])
                if isinstance(item, dict) and not item.get("is_dir")
            ]
            if not files and metadata.get("file_name"):
                files = [{
                    "file_name": metadata.get("file_name"),
                    "size": int(metadata.get("size") or 0),
                    "fs_id": metadata.get("fs_id") or "",
                    "direct_url": metadata.get("direct_url") or "",
                    "stream_url": metadata.get("stream_url") or "",
                }]

            task.file_names = [
                safe_filename(str(item.get("file_name") or "terabox-file"))
                for item in files
            ]
            await self._resolve_missing_file_sizes(files)
            total = sum(int(item.get("size") or 0) for item in files)

            if not task.file_names:
                raise RuntimeError("No files were found in this TeraBox link.")
            if total <= 0:
                raise RuntimeError("Could not determine the actual file size yet.")

            # TeraBox's metadata size is the source file size, not an estimate.
            task.size_bytes = total
        except Exception as exc:
            log.warning(
                "task=%s metadata inspection failed: %s",
                task.task_id,
                self._compact_error(exc),
            )
            task.file_names = []
            task.size_bytes = None
        finally:
            self.inspecting_task = None

        if task.cancel_requested:
            return

        if task.size_bytes is None:
            if task.retry_count < 2:
                task.retry_count += 1
                self.size_inspection_queue.append(task)
                self.size_event.set()
                log.info(
                    "task=%s size inspection retry=%s",
                    task.task_id,
                    task.retry_count,
                )
                return

            log.warning(
                "task=%s could not determine an actual size after retries; dropping from queue",
                task.task_id,
            )
            if self.telegram.is_connected():
                try:
                    await self.telegram.send_message(
                        task.chat_id,
                        "❌ I could not determine the actual file size for this TeraBox link, so it was not added to the download queue.",
                    )
                except Exception:
                    pass
            return

        self._push_download_task(task)
        self.download_event.set()
        log.info(
            "task=%s moved to download queue size=%s ready=%s",
            task.task_id,
            task.size_bytes,
            len(self.download_queue),
        )

    async def _size_inspection_loop(self):
        # Two metadata resolutions can run concurrently. Each completed task
        # is promoted immediately; there is no batch barrier.
        max_parallel = 2
        pending: set[asyncio.Task] = set()

        while not self.stop_event.is_set():
            await self.size_event.wait()
            self.size_event.clear()

            while (
                (self.size_inspection_queue or pending)
                and not self.stop_event.is_set()
            ):
                while (
                    self.size_inspection_queue
                    and len(pending) < max_parallel
                    and not self.stop_event.is_set()
                ):
                    task = self.size_inspection_queue.pop(0)
                    if task.cancel_requested:
                        continue
                    pending.add(asyncio.create_task(self._inspect_one_task(task)))

                if not pending:
                    break

                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for completed in done:
                    try:
                        await completed
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception(
                            "Unhandled metadata inspection task failure"
                        )

        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---------- download queue ----------

    async def _download_queue_loop(self):
        while not self.stop_event.is_set():
            await self.download_event.wait()
            self.download_event.clear()

            while self.download_queue and not self.stop_event.is_set():
                # Do not wait for the remaining metadata-inspection queue.
                # Start immediately with the smallest task whose size is already
                # known, while the size inspector continues working in parallel.
                task = self._next_download_task()
                if not task:
                    break
                if task.cancel_requested:
                    continue

                self.active_task = task
                try:
                    await self._run_job(task)
                except Exception:
                    log.exception("Unhandled job failure task=%s", task.task_id)
                finally:
                    if self.active_task is task:
                        self.active_task = None

    async def _run_job(self, task: QueueTask):
        started = time.monotonic()
        job_id = f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        task.job_id = job_id

        job_dir = DOWNLOADS_DIR / job_id
        unpack_dir = UNPACKED_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        unpack_dir.mkdir(parents=True, exist_ok=True)

        link_counter = self._link_counter_text(task.url)
        job = {
            "id": job_id,
            "taskId": task.task_id,
            "url": task.url,
            "linkCounter": self._link_counter(task.url),
            "status": "resolving",
            "progress": 10,
            "statusText": "Analyzing TeraBox share link...",
            "files": [],
            "chatId": str(task.chat_id),
            "retryCount": task.retry_count,
            "maxRetries": 1,
            "createdAt": int(time.time() * 1000),
            "logs": [
                f"[{datetime.now().strftime('%H:%M:%S')}] Job initialized for {task.url} (link {link_counter})"
            ],
        }
        self.jobs.insert(0, job)
        self.jobs = self.jobs[:50]
        self._save_jobs()

        await self._status(
            job,
            task,
            "🔎 Checking your TeraBox link…",
        )

        try:
            job["status"] = "resolving"
            self._save_jobs()
            resolved = await self.resolver.resolve(task.url, allow_native_download=True)
            files = [
                item for item in (resolved.get("files") or [])
                if isinstance(item, dict) and not item.get("is_dir")
            ]

            top_level = {
                "file_name": resolved.get("file_name") or "terabox-file",
                "size": int(resolved.get("size") or 0),
                "fs_id": resolved.get("fs_id") or "",
                "direct_url": resolved.get("direct_url") or "",
                "stream_url": resolved.get("stream_url") or "",
                "browser_download_path": resolved.get("browser_download_path") or "",
                "duration": int(resolved.get("duration") or 0),
                "sign": resolved.get("sign") or "",
                "timestamp": resolved.get("timestamp") or "",
            }
            if not files and (
                top_level["direct_url"]
                or top_level["stream_url"]
                or top_level["browser_download_path"]
            ):
                files = [top_level]

            files = [
                item
                for item in files
                if item.get("direct_url")
                or item.get("stream_url")
                or item.get("browser_download_path")
            ]

            if not files:
                raise RuntimeError(
                    "No downloadable files were found in this TeraBox link."
                )
            if len(files) > MAX_FILES_PER_LINK:
                raise RuntimeError(
                    f"This link contains too many files. The maximum is {MAX_FILES_PER_LINK}."
                )

            total_source_size = sum(int(item.get("size") or 0) for item in files)
            await self._status(
                job,
                task,
                f"✅ Found {len(files)} file(s), about {format_bytes(total_source_size)}.\n⬇️ Starting download…",
            )

            processed_files: list[dict] = []
            failed_files: list[str] = []

            browser_download_path = str(resolved.get("browser_download_path") or "")
            browser_download_used = False

            for index, source in enumerate(files):
                self._check_cancel(task)

                original_name = safe_filename(
                    str(source.get("file_name") or f"file_{index + 1}")
                )
                expected_size = int(source.get("size") or 0)
                if expected_size > MAX_SOURCE_FILE_SIZE_BYTES:
                    raise RuntimeError(
                        f"{original_name} exceeds MAX_SOURCE_FILE_SIZE_BYTES."
                    )

                job["status"] = "downloading"
                await self._status(
                    job,
                    task,
                    f"⬇️ Downloading {original_name} ({index + 1}/{len(files)})…",
                    progress=20 + round(index * 60 / max(1, len(files))),
                )

                downloaded_path: Path | None = None
                candidate_name = original_name
                used_hls = False

                try:
                    if (
                        browser_download_path
                        and not browser_download_used
                        and (
                            not source.get("fs_id")
                            or str(source.get("fs_id")) == str(resolved.get("fs_id") or "")
                            or index == 0
                        )
                    ):
                        path = Path(browser_download_path)
                        if path.exists():
                            target = job_dir / f"{index}_{safe_filename(candidate_name)}"
                            shutil.move(str(path), str(target))
                            downloaded_path = target
                            browser_download_used = True

                    if downloaded_path is None and source.get("direct_url"):
                        downloaded_path, _ = await self._download_direct(
                            task,
                            str(source["direct_url"]),
                            candidate_name,
                            expected_size=expected_size,
                            cookies=str(resolved.get("cookies") or ""),
                            referer=str(
                                resolved.get("referer_url")
                                or "https://www.terabox.app/"
                            ),
                        )

                    if downloaded_path is None and source.get("stream_url"):
                        if not candidate_name.lower().endswith(".mp4"):
                            candidate_name = re.sub(r"\.[^.]+$", "", candidate_name) + ".mp4"
                        downloaded_path, _ = await self._download_hls(
                            job,
                            task,
                            str(source["stream_url"]),
                            candidate_name,
                            resolved,
                            source,
                        )
                        used_hls = True

                    if downloaded_path is None:
                        raise RuntimeError("The file could not be downloaded.")

                    actual_size = downloaded_path.stat().st_size
                    if actual_size <= 0:
                        raise RuntimeError("Downloaded file is empty.")
                    if expected_size and not used_hls and actual_size != expected_size:
                        raise RuntimeError(
                            f"Downloaded size mismatch: expected {expected_size} bytes, received {actual_size} bytes."
                        )

                    header = downloaded_path.read_bytes()[:64]
                    detected = detect_extension_from_buffer(header)
                    if detected and not candidate_name.lower().endswith(detected):
                        candidate_name = candidate_name + detected
                        renamed = downloaded_path.with_name(
                            f"{downloaded_path.stem}_{safe_filename(candidate_name)}"
                        )
                        downloaded_path.rename(renamed)
                        downloaded_path = renamed

                    suffix = Path(candidate_name).suffix.lower()
                    is_zip = suffix == ".zip" or detected == ".zip"
                    is_video = suffix in VIDEO_EXTENSIONS

                    if is_zip and downloaded_path.stat().st_size > MAX_ZIP_SIZE_BYTES:
                        raise RuntimeError(
                            "This ZIP archive is too large to unpack safely."
                        )

                    if is_zip:
                        await self._status(
                            job,
                            task,
                            f"📦 Unpacking {candidate_name}…",
                            progress=70,
                        )
                        extracted = await asyncio.to_thread(
                            self._unpack_zip,
                            downloaded_path,
                            unpack_dir / str(index),
                        )
                        if extracted:
                            for extracted_file in extracted:
                                ext = extracted_file.suffix.lower()
                                processed_files.append(
                                    {
                                        "filename": extracted_file.name,
                                        "sizeBytes": extracted_file.stat().st_size,
                                        "sizeFormatted": format_bytes(extracted_file.stat().st_size),
                                        "path": str(extracted_file),
                                        "isVideo": ext in VIDEO_EXTENSIONS,
                                        "isZip": False,
                                    }
                                )
                            continue

                    processed_files.append(
                        {
                            "filename": candidate_name,
                            "sizeBytes": downloaded_path.stat().st_size,
                            "sizeFormatted": format_bytes(downloaded_path.stat().st_size),
                            "path": str(downloaded_path),
                            "isVideo": is_video,
                            "isZip": is_zip,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Could not process %s: %s",
                        original_name,
                        self._compact_error(exc),
                    )
                    if downloaded_path and downloaded_path.exists():
                        try:
                            if downloaded_path.parent not in {job_dir, unpack_dir}:
                                downloaded_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    failed_files.append(original_name)

            if not processed_files:
                raise RuntimeError(
                    "None of the files in this TeraBox link could be downloaded."
                )

            job["files"] = processed_files
            job["status"] = "uploading"
            job["progress"] = 85
            job["statusText"] = "Uploading files to Telegram..."
            self._save_jobs()

            if failed_files:
                await self.telegram.send_message(
                    task.chat_id,
                    "⚠️ Could not download: " + ", ".join(failed_files[:15]),
                )

            for index, processed in enumerate(processed_files):
                self._check_cancel(task)
                path = Path(str(processed["path"]))
                if not path.exists():
                    continue

                await self._status(
                    job,
                    task,
                    f"📤 Sending {processed['filename']} ({index + 1}/{len(processed_files)})…",
                    progress=85 + round(index * 13 / max(1, len(processed_files))),
                )
                await self._upload_processed(
                    job,
                    task,
                    path,
                    str(processed["filename"]),
                    bool(processed.get("isVideo")),
                    int(processed.get("sizeBytes") or path.stat().st_size),
                    index,
                    len(processed_files),
                    job_dir,
                )

            elapsed = round(time.monotonic() - started, 2)
            job["status"] = "completed"
            job["progress"] = 100
            job["statusText"] = "Completed successfully"
            job["completedAt"] = int(time.time() * 1000)
            job["logs"].append(
                f"[{datetime.now().strftime('%H:%M:%S')}] Finished job processing in {elapsed}s"
            )
            self._save_jobs()

            await self._status(
                job,
                task,
                f"✅ Sent successfully: {len(processed_files)} file(s)\n⏱ {elapsed}s",
                progress=100,
                include_cancel_button=False,
            )
            log.info(
                "job=%s completed files=%s elapsed=%ss",
                job_id,
                len(processed_files),
                elapsed,
            )
        except asyncio.CancelledError:
            job["status"] = "failed"
            job["error"] = "Cancelled by worker shutdown"
            job["statusText"] = "Cancelled by worker shutdown"
            self._save_jobs()
            raise
        except Exception as exc:
            error_text = self._compact_error(exc)
            job["status"] = "failed"
            job["error"] = error_text
            job["statusText"] = f"Failed: {error_text}"
            job["logs"].append(
                f"[{datetime.now().strftime('%H:%M:%S')}] Error: {error_text}"
            )
            self._save_jobs()

            if task.cancel_requested:
                await self._status(
                    job,
                    task,
                    f"🛑 Download cancelled.\n🔗 {task.url}",
                    include_cancel_button=False,
                )
            else:
                await self._status(
                    job,
                    task,
                    f"❌ Unable to download this link.\n{error_text}",
                    include_cancel_button=False,
                )
            log.exception("job=%s failed", job_id)
        finally:
            self._cleanup_job_dir(job_dir, unpack_dir)

    async def _download_direct(
        self,
        task: QueueTask,
        url: str,
        filename: str,
        *,
        expected_size: int = 0,
        cookies: str = "",
        referer: str = "",
    ) -> tuple[Path, int]:
        session = self.http_session
        if session is None or session.closed:
            raise RuntimeError("HTTP session is not available.")

        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=120)
        suffix = Path(filename).suffix or ".bin"
        fd, temp_name = tempfile.mkstemp(
            prefix=f"terabox-{task.task_id}-",
            suffix=suffix,
            dir=str(DOWNLOADS_DIR / "active"),
        )
        os.close(fd)
        path = Path(temp_name)
        path.parent.mkdir(parents=True, exist_ok=True)

        base_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": referer or "https://www.terabox.app/",
            **({"Cookie": cookies} if cookies else {}),
        }

        total = 0
        content_length = 0
        blocked_types = {
            "text/html",
            "text/plain",
            "application/json",
            "application/xml",
            "text/xml",
        }

        try:
            # First request discovers whether the CDN supports byte ranges and
            # gives us the authoritative size. We retain the connection in the
            # shared pool for all subsequent range requests.
            async with session.get(
                url,
                headers=base_headers,
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length") or 0)
                accept_ranges = (
                    response.headers.get("Accept-Ranges", "").lower() == "bytes"
                )
                content_type = (
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].strip().lower()

                if content_type in blocked_types:
                    raise RuntimeError(
                        f"Resolved URL returned a non-file response ({content_type})."
                    )

                authoritative_size = max(expected_size, content_length)
                log.info(
                    "task=%s direct download HTTP=%s expected=%s content_length=%s "
                    "ranges=%s type=%s final=%s",
                    task.task_id,
                    response.status,
                    expected_size,
                    content_length,
                    accept_ranges,
                    content_type or "<missing>",
                    response.url,
                )

                if not accept_ranges or authoritative_size <= 0:
                    # Fallback to one sequential stream when the CDN does not
                    # advertise range support.
                    with path.open("wb") as output:
                        async for chunk in response.content.iter_chunked(4 * 1024 * 1024):
                            self._check_cancel(task)
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(
                                    f"File exceeds MAX_DOWNLOAD_BYTES ({MAX_DOWNLOAD_BYTES})."
                                )
                            output.write(chunk)
                    if total <= 0:
                        raise RuntimeError("TeraBox direct URL returned an empty file.")
                    if expected_size and total != expected_size:
                        raise RuntimeError(
                            f"TeraBox download size mismatch: expected {expected_size} bytes, received {total} bytes."
                        )
                    return path, total

            size = authoritative_size
            if size > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"File exceeds MAX_DOWNLOAD_BYTES ({MAX_DOWNLOAD_BYTES})."
                )

            try:
                workers = max(
                    1,
                    min(6, int(os.getenv("DIRECT_DOWNLOAD_CONCURRENCY", "4"))),
                )
                range_mb = max(
                    2,
                    min(16, int(os.getenv("DIRECT_DOWNLOAD_RANGE_MB", "8"))),
                )
            except (TypeError, ValueError):
                workers = 4
                range_mb = 8

            chunk_size = range_mb * 1024 * 1024
            ranges = [
                (start, min(size - 1, start + chunk_size - 1))
                for start in range(0, size, chunk_size)
            ]

            concurrency = min(workers, len(ranges))
            write_handle = await asyncio.to_thread(path.open, "wb")
            write_handle.close()
            write_offsets = asyncio.Lock()

            async def fetch_range(range_start: int, range_end: int) -> int:
                for attempt in range(4):
                    self._check_cancel(task)
                    try:
                        headers = dict(base_headers)
                        headers["Range"] = f"bytes={range_start}-{range_end}"
                        async with session.get(
                            url,
                            headers=headers,
                            allow_redirects=True,
                            timeout=timeout,
                        ) as response:
                            if response.status not in (200, 206):
                                raise RuntimeError(
                                    f"Range HTTP {response.status} for {range_start}-{range_end}"
                                )

                            data = await response.read()
                            expected = range_end - range_start + 1
                            if response.status == 206 and len(data) != expected:
                                raise RuntimeError(
                                    f"Short range response: expected {expected}, got {len(data)}"
                                )

                            # Some CDNs silently ignore Range and return the full
                            # object. Never let that overwrite the requested range.
                            if response.status == 200 and len(data) != size:
                                raise RuntimeError(
                                    f"Unexpected full response length {len(data)} for range request"
                                )
                            if response.status == 200 and range_start != 0:
                                raise RuntimeError(
                                    "CDN ignored Range header for a non-zero offset."
                                )

                            offset = range_start
                            async with write_offsets:
                                with path.open("r+b") as output:
                                    output.seek(offset)
                                    output.write(
                                        data
                                        if response.status == 206
                                        else data[: range_end - range_start + 1]
                                    )
                            return len(data) if response.status == 206 else (
                                range_end - range_start + 1
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if attempt >= 3:
                            raise
                        delay = 0.5 * (2 ** attempt)
                        log.warning(
                            "task=%s direct range %s-%s failed attempt=%s: %s; retrying %.1fs",
                            task.task_id,
                            range_start,
                            range_end,
                            attempt + 1,
                            self._compact_error(exc),
                            delay,
                        )
                        await asyncio.sleep(delay)

                raise RuntimeError("unreachable")

            completed = 0
            total_ranges = len(ranges)
            bytes_done = 0
            bytes_lock = asyncio.Lock()

            semaphore = asyncio.Semaphore(concurrency)

            async def worker_range(item):
                nonlocal completed, bytes_done
                async with semaphore:
                    got = await fetch_range(*item)
                async with bytes_lock:
                    completed += 1
                    bytes_done += got
                    if completed == total_ranges or completed % max(1, total_ranges // 10) == 0:
                        log.info(
                            "task=%s direct ranges=%s/%s bytes=%s/%s",
                            task.task_id,
                            completed,
                            total_ranges,
                            bytes_done,
                            size,
                        )
                return got

            await asyncio.gather(*(worker_range(item) for item in ranges))

            total = path.stat().st_size
            if total != size:
                raise RuntimeError(
                    f"Ranged download size mismatch: expected {size} bytes, received {total} bytes."
                )
            if expected_size and total != expected_size:
                raise RuntimeError(
                    f"TeraBox download size mismatch: expected {expected_size} bytes, received {total} bytes."
                )
            return path, total
        except Exception:
            path.unlink(missing_ok=True)
            raise

    async def _download_hls(
        self,
        job: dict,
        task: QueueTask,
        stream_url: str,
        filename: str,
        resolved: dict,
        source: dict,
    ) -> tuple[Path, int]:
        suffix = Path(filename).suffix or ".mp4"
        active_dir = DOWNLOADS_DIR / "active"
        active_dir.mkdir(parents=True, exist_ok=True)

        # Do not pre-create the final output file. FFmpeg owns the final
        # destination and will atomically create/replace it after remuxing.
        # Pre-creating a zero-byte output can cause inconsistent behavior on
        # some FFmpeg/filesystem combinations.
        output_name = f"terabox-hls-{task.task_id}-{uuid.uuid4().hex}{suffix}"
        path = active_dir / output_name

        last_status = 0.0
        last_percent = -1

        async def progress(percent: int, current: int, total: int):
            nonlocal last_status, last_percent
            self._check_cancel(task)
            now = time.monotonic()
            if percent == 100 or (
                percent >= last_percent + 10 and now - last_status >= 8
            ):
                last_percent = percent
                last_status = now
                await self._status(
                    job,
                    task,
                    f"⬇️ Downloading {filename}: {percent}% ({current}/{total} chunks)",
                )

        try:
            log.info(
                "task=%s starting HLS download output=%s",
                task.task_id,
                path,
            )
            await download_m3u8_stream(
                stream_url,
                path,
                referer_url=str(
                    resolved.get("referer_url") or "https://www.terabox.app/"
                ),
                cookie_header=str(resolved.get("cookies") or ""),
                duration=int(source.get("duration") or resolved.get("duration") or 0),
                share_id=str(resolved.get("share_id") or ""),
                uk=str(resolved.get("uk") or ""),
                sign=str(source.get("sign") or resolved.get("sign") or ""),
                timestamp=str(
                    source.get("timestamp") or resolved.get("timestamp") or ""
                ),
                fs_id=str(source.get("fs_id") or resolved.get("fs_id") or ""),
                randsk=str(resolved.get("randsk") or ""),
                progress=progress,
                http_session=self.http_session,
            )
            if not path.exists():
                raise RuntimeError(
                    f"HLS downloader returned successfully but output file is missing: {path}"
                )
            size = path.stat().st_size
            if size <= 0:
                raise RuntimeError("M3U8 download produced an empty output file.")
            if size > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"File exceeds MAX_DOWNLOAD_BYTES ({MAX_DOWNLOAD_BYTES})."
                )
            return path, size
        except Exception:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _unpack_zip(zip_path: Path, target_dir: Path) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        used_names: set[str] = set()

        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir() or member.filename.startswith("__MACOSX/"):
                    continue

                filename = safe_filename(Path(member.filename).name)
                if not filename:
                    continue

                base = Path(filename).stem
                suffix = Path(filename).suffix
                index = 1
                unique = filename
                while unique in used_names:
                    unique = f"{base}_{index}{suffix}"
                    index += 1
                used_names.add(unique)

                output = target_dir / unique
                with archive.open(member, "r") as source, output.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                extracted.append(output)

        return extracted

    # ---------- telegram uploads ----------

    async def _upload_processed(
        self,
        job: dict,
        task: QueueTask,
        path: Path,
        filename: str,
        is_video: bool,
        size_bytes: int,
        index: int,
        total_files: int,
        job_dir: Path,
    ) -> None:
        caption_prefix = self._link_counter_text(task.url)

        if size_bytes <= MAX_TELEGRAM_FILE_SIZE:
            await self._send_via_mtproto(
                task.chat_id,
                path,
                filename,
                (
                    f"🎬 [Video Preview] {caption_prefix} {filename} ({format_bytes(size_bytes)})"
                    if is_video
                    else f"📄 [{index + 1}/{total_files}] {caption_prefix} {filename} ({format_bytes(size_bytes)})"
                ),
                is_video=is_video,
            )
            return

        await self.telegram.send_message(
            task.chat_id,
            f"📦 Large file detected: {filename} ({format_bytes(size_bytes)}). Sending…",
        )

        if size_bytes <= MAX_MT_PROTO_FILE_SIZE:
            try:
                await self._send_via_mtproto(
                    task.chat_id,
                    path,
                    filename,
                    f"🎬 [Video Preview] {caption_prefix} {filename} ({format_bytes(size_bytes)})"
                    if is_video
                    else f"📦 {caption_prefix} {filename} ({format_bytes(size_bytes)})",
                    is_video=is_video,
                )
                return
            except Exception as exc:
                log.warning(
                    "MTProto direct upload failed for %s; falling back to parts: %s",
                    filename,
                    self._compact_error(exc),
                )

        parts = (
            await split_video(path, job_dir, MAX_TELEGRAM_FILE_SIZE)
            if is_video
            else split_binary_file(path, job_dir, MAX_TELEGRAM_FILE_SIZE)
        )
        if len(parts) <= 1:
            raise RuntimeError("Large-file split fallback produced no smaller parts.")

        for part_index, part in enumerate(parts, 1):
            self._check_cancel(task)
            part_size = part.stat().st_size
            caption = (
                f"🎬 [Video Part {part_index}/{len(parts)}] "
                f"{caption_prefix} {part.name} ({format_bytes(part_size)})"
                if is_video
                else f"📦 [Part {part_index}/{len(parts)}] "
                f"{caption_prefix} {part.name} ({format_bytes(part_size)})"
            )
            await self._send_via_mtproto(
                task.chat_id,
                part,
                part.name,
                caption,
                is_video=is_video,
            )

    async def _send_via_mtproto(
        self,
        chat_id: int,
        path: Path,
        filename: str,
        caption: str,
        *,
        is_video: bool,
    ):
        if not self.telegram.is_connected():
            raise RuntimeError("Telegram MTProto client is not connected.")

        if not path.exists():
            raise RuntimeError(f"File does not exist: {path}")

        log.info(
            "Uploading via Telethon MTProto: %s (%s bytes, document=%s)",
            filename,
            path.stat().st_size,
            not is_video,
        )
        await self.telegram.send_file(
            chat_id,
            str(path),
            caption=caption,
            force_document=not is_video,
            supports_streaming=is_video,
        )

    # ---------- status / utilities ----------

    async def _status(
        self,
        job: dict,
        task: QueueTask,
        text: str,
        *,
        progress: int | None = None,
        include_cancel_button: bool = True,
    ):
        if progress is not None:
            job["progress"] = max(0, min(100, int(progress)))
        job["statusText"] = text
        job["logs"].append(
            f"[{datetime.now().strftime('%H:%M:%S')}] {text.replace(chr(10), ' ')[:300]}"
        )
        self._save_jobs()

        if not self.telegram.is_connected():
            return

        buttons = (
            [[Button.inline("🛑 Cancel", data=f"cancel:{task.task_id}".encode())]]
            if include_cancel_button
            else None
        )

        try:
            message_id = int(job.get("statusMessageId") or 0)
            if message_id:
                await self.telegram.edit_message(
                    task.chat_id,
                    message_id,
                    text,
                    buttons=buttons,
                )
            else:
                message = await self.telegram.send_message(
                    task.chat_id,
                    text,
                    buttons=buttons,
                )
                job["statusMessageId"] = int(message.id)
                self._save_jobs()
        except Exception as exc:
            log.debug("Could not update status message for %s: %s", job.get("id"), exc)

    def _check_cancel(self, task: QueueTask) -> None:
        if task.cancel_requested:
            raise RuntimeError("Download cancelled by user")

    @staticmethod
    def _compact_error(exc: Exception) -> str:
        value = str(exc).strip().replace("\n", " ")
        return value[:800] or exc.__class__.__name__

    @staticmethod
    def _cleanup_job_dir(job_dir: Path, unpack_dir: Path) -> None:
        for directory in (job_dir, unpack_dir):
            try:
                shutil.rmtree(directory, ignore_errors=True)
            except Exception:
                pass

        active_dir = DOWNLOADS_DIR / "active"
        try:
            if active_dir.exists() and not any(active_dir.iterdir()):
                active_dir.rmdir()
        except OSError:
            pass


app = FastAPI(title="TeraBox Oracle VPS Worker")
worker: Worker | None = None


@app.on_event("startup")
async def startup():
    global worker
    worker = Worker()
    await worker.start()


@app.on_event("shutdown")
async def shutdown():
    if worker:
        await worker.stop()


@app.get("/health")
async def health():
    if not worker:
        return {"status": "starting"}

    browser_ready = bool(worker.resolver.browser)
    telegram_ready = worker.telegram.is_connected()
    return {
        "status": "ok" if browser_ready and telegram_ready else "starting",
        "queue_size": worker._queued_count(),
        "browser": browser_ready,
        "telegram_connected": telegram_ready,
        "telegram_authorization_in_progress": bool(
            worker.telegram_task and not worker.telegram_task.done()
        ),
        "active_task": worker.active_task.task_id[:8] if worker.active_task else None,
        "size_inspection": len(worker.size_inspection_queue) + (1 if worker.inspecting_task else 0),
    }
