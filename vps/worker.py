from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from fastapi import FastAPI
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import SQLiteSession

from .browser_resolver import TeraBoxBrowserResolver
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
}

@dataclass(slots=True)
class Job:
    job_id: str
    chat_id: int
    url: str

class Worker:
    def __init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        try:
            self.api_id = int(os.getenv("API_ID", "0"))
        except ValueError as exc:
            raise RuntimeError("API_ID must be numeric.") from exc
        self.api_hash = os.getenv("API_HASH", "").strip()
        self.max_download_bytes = int(
            os.getenv("MAX_DOWNLOAD_BYTES", str(10 * 1024 * 1024 * 1024))
        )

        if not self.bot_token or not self.api_id or not self.api_hash:
            raise RuntimeError("BOT_TOKEN, API_ID and API_HASH are required.")

        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.resolver = TeraBoxBrowserResolver(max_concurrent=1)

        # Persist MTProto authorization across Docker container restarts.
        self.session_dir = Path(os.getenv("TELETHON_SESSION_DIR", "/worker/data"))
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = str(self.session_dir / "terabox_bot")
        self.telegram = TelegramClient(
            SQLiteSession(self.session_path),
            self.api_id,
            self.api_hash,
        )
        self.task: asyncio.Task | None = None
        self.telegram_task: asyncio.Task | None = None

    async def start(self):
        log.info("Starting Chromium resolver...")
        await self.resolver.start()

        log.info("Removing any legacy Telegram Bot API webhook...")
        await self._delete_bot_api_webhook()

        # Telegram authorization can temporarily return FloodWait. Do not
        # block FastAPI startup or cause Hypercorn to restart the container.
        self.telegram_task = asyncio.create_task(
            self._connect_telegram_with_retry()
        )

        self.task = asyncio.create_task(self._queue_loop())
        log.info(
            "VPS worker started; Telegram authorization is running in the background."
        )

    async def _connect_telegram_with_retry(self):
        registered = False

        while True:
            try:
                log.info("Starting Telegram MTProto bot client on VPS...")
                await self.telegram.start(bot_token=self.bot_token)

                if not registered:
                    self.telegram.add_event_handler(
                        self._on_message,
                        events.NewMessage(incoming=True),
                    )
                    registered = True

                me = await self.telegram.get_me()
                log.info(
                    "Telegram bot connected: username=@%s id=%s",
                    getattr(me, "username", None),
                    getattr(me, "id", None),
                )
                log.info(
                    "VPS worker ready; Telegram intake is running directly on Oracle."
                )
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
                log.exception(
                    "Telegram MTProto connection failed; retrying in 30 seconds."
                )
                if self.telegram.is_connected():
                    await self.telegram.disconnect()
                await asyncio.sleep(30)

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

    async def stop(self):
        for task in (self.task, self.telegram_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self.task = None
        self.telegram_task = None

        if self.telegram.is_connected():
            await self.telegram.disconnect()

        await self.resolver.stop()

    async def _on_message(self, event):
        text = (event.raw_text or "").strip()
        chat_id = event.chat_id

        if not chat_id:
            return

        if text in {"/start", "/help"}:
            await event.reply(
                "Send me a TeraBox link and I will resolve and send the file."
            )
            return

        urls = re.findall(r"https?://\S+", text)
        if not urls:
            return

        url = urls[0].rstrip(").,>")
        if not self._is_terabox_url(url):
            return

        job = Job(
            job_id=uuid.uuid4().hex,
            chat_id=int(chat_id),
            url=url,
        )

        log.info(
            "telegram message accepted job=%s chat_id=%s queue_size_before=%s url=%s",
            job.job_id,
            job.chat_id,
            self.queue.qsize(),
            job.url,
        )

        await event.reply(
            f"⏳ Queued your TeraBox link. Job: {job.job_id[:8]}"
        )
        await self.queue.put(job)

    async def _queue_loop(self):
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(job)
            except Exception:
                log.exception("Unhandled job failure id=%s", job.job_id)
                try:
                    await self.telegram.send_message(
                        job.chat_id,
                        f"❌ Job {job.job_id[:8]} failed unexpectedly.",
                    )
                except Exception:
                    log.exception("Could not send unexpected-failure message id=%s", job.job_id)
            finally:
                self.queue.task_done()

    async def _run_job(self, job: Job):
        started = time.monotonic()
        temp_path: Path | None = None

        await self._status(job, "🔎 Resolving TeraBox link…")
        try:
            log.info("job=%s resolving url=%s", job.job_id, job.url)

            resolved = await self.resolver.resolve(job.url)
            filename = self._safe_filename(
                resolved.get("file_name") or "terabox-file"
            )
            size = int(resolved.get("size") or 0)
            direct_url = str(resolved.get("direct_url") or "").strip()
            stream_url = str(resolved.get("stream_url") or "").strip()
            browser_download_path = str(
                resolved.get("browser_download_path") or ""
            ).strip()
            download_mode = str(resolved.get("download_mode") or "").strip()

            if not direct_url and not stream_url and not browser_download_path:
                raise RuntimeError(
                    "Resolver returned neither a direct download URL nor an HLS stream URL."
                )

            log.info(
                "job=%s resolved file=%s size=%s mode=%s resolve_ms=%s",
                job.job_id,
                filename,
                size,
                download_mode or ("direct" if direct_url else "stream"),
                resolved.get("resolve_ms"),
            )

            await self._status(
                job,
                f"✅ Resolved: {filename}\n⬇️ Starting download…",
            )

            if browser_download_path:
                browser_path = Path(browser_download_path)
                if not browser_path.exists():
                    raise RuntimeError(
                        "Chromium reported a downloaded file, but the temporary file is missing."
                    )
                temp_path = browser_path
                downloaded_size = browser_path.stat().st_size
                if downloaded_size <= 0:
                    raise RuntimeError("Chromium downloaded an empty file.")
                if size and downloaded_size != size:
                    raise RuntimeError(
                        f"Browser download size mismatch: expected {size} bytes, "
                        f"received {downloaded_size} bytes."
                    )
                log.info(
                    "job=%s using native browser download file=%s size=%s",
                    job.job_id,
                    filename,
                    downloaded_size,
                )
            elif stream_url and not direct_url:
                temp_path, downloaded_size = await self._download_stream(
                    job,
                    stream_url,
                    filename,
                    resolved,
                )
            else:
                temp_path, downloaded_size = await self._download(
                    job, direct_url, filename, expected_size=size
                )

            await self._status(job, f"📤 Uploading {filename} to Telegram…")

            await self._send_file(job.chat_id, temp_path, filename)

            elapsed = round(time.monotonic() - started, 2)
            log.info(
                "job=%s completed filename=%s size=%s elapsed=%ss",
                job.job_id,
                filename,
                downloaded_size,
                elapsed,
            )

            await self._status(
                job,
                f"✅ Sent successfully: {filename}\n⏱ {elapsed}s",
            )
        except Exception as exc:
            log.exception("job=%s failed", job.job_id)
            await self._status(
                job,
                f"❌ TeraBox delivery failed: {self._compact_error(exc)}",
            )
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    log.warning("Could not remove temp file %s", temp_path)

    async def _download(self, job: Job, url: str, filename: str, *, expected_size: int = 0) -> tuple[Path, int]:
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=120)
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"terabox-{job.job_id}-",
            suffix=Path(filename).suffix or ".bin",
            delete=False,
            dir="/tmp",
        )
        path = Path(tmp.name)
        total = 0
        last_log = 0
        last_messenger_update = 0
        last_messenger_percent = -1

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
                    allow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("Content-Length") or 0)
                    content_type = (
                        response.headers.get("Content-Type")
                        or ""
                    ).split(";", 1)[0].strip().lower()
                    declared = max(expected_size, content_length)

                    log.info(
                        "job=%s downloading HTTP=%s expected_size=%s content_length=%s content_type=%s final_url=%s",
                        job.job_id,
                        response.status,
                        expected_size,
                        content_length,
                        content_type or "<missing>",
                        response.url,
                    )

                    blocked_types = {
                        "text/html",
                        "text/plain",
                        "application/json",
                        "application/xml",
                        "text/xml",
                    }
                    if content_type in blocked_types:
                        raise RuntimeError(
                            "Resolved URL returned a non-file response "
                            f"({content_type}); refusing to upload it."
                        )

                    with tmp:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            total += len(chunk)
                            if total > self.max_download_bytes:
                                raise RuntimeError(
                                    f"File exceeds MAX_DOWNLOAD_BYTES ({self.max_download_bytes})."
                                )
                            tmp.write(chunk)

                            now = time.monotonic()
                            if now - last_log >= 5:
                                last_log = now
                                if declared:
                                    percent = min(100, int(total * 100 / declared))
                                    log.info(
                                        "job=%s download_progress=%s/%s bytes (%s%%)",
                                        job.job_id, total, declared, percent
                                    )
                                    if (
                                        percent >= last_messenger_percent + 10
                                        and now - last_messenger_update >= 10
                                    ):
                                        last_messenger_percent = percent
                                        last_messenger_update = now
                                        await self._status(
                                            job,
                                            f"⬇️ Downloading {filename}: {percent}%",
                                        )
                                else:
                                    log.info(
                                        "job=%s download_progress=%s bytes",
                                        job.job_id, total
                                    )
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

        if total <= 0:
            path.unlink(missing_ok=True)
            raise RuntimeError("TeraBox direct URL returned an empty file.")

        if expected_size and total != expected_size:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"TeraBox download size mismatch: expected {expected_size} bytes, "
                f"received {total} bytes."
            )

        return path, total

    async def _download_stream(
        self,
        job: Job,
        stream_url: str,
        filename: str,
        resolved: dict,
    ) -> tuple[Path, int]:
        suffix = Path(filename).suffix or ".mp4"
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"terabox-{job.job_id}-",
            suffix=suffix,
            delete=False,
            dir="/tmp",
        )
        path = Path(tmp.name)
        tmp.close()

        last_messenger_update = 0.0
        last_percent = -1

        async def progress(percent: int, current: int, total: int):
            nonlocal last_messenger_update, last_percent
            now = time.monotonic()
            if (
                percent == 100
                or percent >= last_percent + 10
                and now - last_messenger_update >= 10
            ):
                last_percent = percent
                last_messenger_update = now
                await self._status(
                    job,
                    f"⬇️ Downloading {filename}: {percent}% "
                    f"({current}/{total} chunks)",
                )

        try:
            downloaded_size, chunk_count = await download_m3u8_stream(
                stream_url,
                path,
                referer_url=str(
                    resolved.get("referer_url")
                    or "https://www.terabox.app/"
                ),
                cookie_header=str(resolved.get("cookies") or ""),
                duration=int(resolved.get("duration") or 0),
                share_id=str(
                    resolved.get("share_id")
                    or resolved.get("shareid")
                    or ""
                ),
                uk=str(resolved.get("uk") or ""),
                sign=str(resolved.get("sign") or ""),
                timestamp=str(resolved.get("timestamp") or ""),
                fs_id=str(resolved.get("fs_id") or ""),
                randsk=str(resolved.get("randsk") or ""),
                progress=progress,
            )
            log.info(
                "job=%s HLS download completed chunks=%s bytes=%s",
                job.job_id,
                chunk_count,
                downloaded_size,
            )
            return path, downloaded_size
        except Exception:
            path.unlink(missing_ok=True)
            raise

    async def _send_file(self, chat_id: int, path: Path, filename: str):
        streaming_extensions = {
            ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
            ".mp3", ".m4a", ".aac",
        }
        supports_streaming = path.suffix.lower() in streaming_extensions

        await self.telegram.send_file(
            chat_id,
            str(path),
            caption=filename,
            force_document=not supports_streaming,
            supports_streaming=supports_streaming,
        )

    async def _status(self, job: Job, text: str):
        try:
            await self.telegram.send_message(job.chat_id, text)
            log.info(
                "job=%s status_sent chat_id=%s text=%s",
                job.job_id,
                job.chat_id,
                text.replace("\n", " ")[:240],
            )
        except Exception:
            log.exception("job=%s could not send Telegram status", job.job_id)

    @staticmethod
    def _is_terabox_url(value: str) -> bool:
        try:
            host = (urlparse(value.strip()).hostname or "").lower()
        except ValueError:
            return False
        return host in TERABOX_HOSTS or host.endswith(".terabox.com")

    @staticmethod
    def _compact_error(exc: Exception) -> str:
        text = str(exc).strip().replace("\n", " ")
        return text[:800] or exc.__class__.__name__

    @staticmethod
    def _safe_filename(value: str) -> str:
        clean = Path(value.replace("\\", "/")).name.strip()
        clean = "".join(
            char if char.isprintable() and char not in "\x00\r\n" else "_"
            for char in clean
        )
        return clean[:240] or "terabox-file"


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
        "status": "ok" if browser_ready else "starting",
        "queue_size": worker.queue.qsize(),
        "browser": browser_ready,
        "telegram_connected": telegram_ready,
        "telegram_authorization_in_progress": bool(
            worker.telegram_task and not worker.telegram_task.done()
        ),
    }
