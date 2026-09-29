from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.sessions import StringSession

from .browser_resolver import TeraBoxBrowserResolver

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("terabox-vps-worker")


class Job(BaseModel):
    job_id: str = Field(min_length=4, max_length=100)
    chat_id: int
    url: str
    callback_url: str


class Worker:
    def __init__(self):
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.api_id = int(os.getenv("API_ID", "0"))
        self.api_hash = os.getenv("API_HASH", "").strip()
        self.worker_secret = os.getenv("VPS_WORKER_SECRET", "").strip()
        self.max_download_bytes = int(
            os.getenv("MAX_DOWNLOAD_BYTES", str(10 * 1024 * 1024 * 1024))
        )

        if not self.bot_token or not self.api_id or not self.api_hash:
            raise RuntimeError("BOT_TOKEN, API_ID and API_HASH are required.")
        if not self.worker_secret:
            raise RuntimeError("VPS_WORKER_SECRET is required.")

        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.resolver = TeraBoxBrowserResolver(max_concurrent=1)
        self.telegram = TelegramClient(
            StringSession(),
            self.api_id,
            self.api_hash,
        )
        self.task: asyncio.Task | None = None

    async def start(self):
        log.info("Starting Chromium resolver...")
        await self.resolver.start()
        log.info("Starting Telethon MTProto client on VPS...")
        await self.telegram.start(bot_token=self.bot_token)
        self.task = asyncio.create_task(self._queue_loop())
        log.info("VPS worker ready.")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

        await self.telegram.disconnect()
        await self.resolver.stop()

    async def enqueue(self, job: Job):
        await self.queue.put(job)
        log.info(
            "job queued id=%s queue_size=%s chat_id=%s",
            job.job_id,
            self.queue.qsize(),
            job.chat_id,
        )

    async def _queue_loop(self):
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(job)
            except Exception:
                log.exception("Unhandled job failure id=%s", job.job_id)
            finally:
                self.queue.task_done()

    async def _callback(self, job: Job, status: str, message: str = "", **extra):
        payload = {
            "job_id": job.job_id,
            "chat_id": job.chat_id,
            "status": status,
            "message": message[:1000],
            **extra,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=15, connect=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(job.callback_url, json=payload) as response:
                    body = await response.text()
                    if response.status >= 400:
                        log.warning(
                            "callback failed id=%s HTTP=%s body=%s",
                            job.job_id,
                            response.status,
                            body[:300],
                        )
        except Exception:
            log.exception("callback exception id=%s status=%s", job.job_id, status)

    async def _run_job(self, job: Job):
        started = time.monotonic()
        temp_path: Path | None = None

        await self._callback(job, "resolving", "Opening TeraBox in Chromium.")
        log.info("job=%s resolving url=%s", job.job_id, job.url)

        try:
            resolved = await self.resolver.resolve(job.url)
            filename = self._safe_filename(
                resolved.get("file_name") or "terabox-file"
            )
            size = int(resolved.get("size") or 0)
            direct_url = str(resolved.get("direct_url") or "").strip()

            if not direct_url:
                raise RuntimeError("Resolver returned no direct download URL.")

            log.info(
                "job=%s resolved file=%s size=%s resolve_ms=%s",
                job.job_id,
                filename,
                size,
                resolved.get("resolve_ms"),
            )

            await self._callback(
                job,
                "resolved",
                "TeraBox resolved successfully.",
                file_name=filename,
                size=size,
            )

            temp_path, downloaded_size = await self._download(
                job, direct_url, filename, expected_size=size
            )

            await self._callback(
                job,
                "uploading",
                f"Uploading {filename} to Telegram.",
                file_name=filename,
                size=downloaded_size,
            )

            await self._send_file(
                job.chat_id,
                temp_path,
                filename,
            )

            elapsed = round(time.monotonic() - started, 2)
            log.info(
                "job=%s completed filename=%s elapsed=%ss",
                job.job_id,
                filename,
                elapsed,
            )
            await self._callback(
                job,
                "completed",
                f"Delivered in {elapsed}s.",
                file_name=filename,
                size=downloaded_size,
            )
        except Exception as exc:
            log.exception("job=%s failed", job.job_id)
            await self._callback(
                job,
                "failed",
                str(exc),
            )
        finally:
            if temp_path:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    log.warning("Could not remove temp file %s", temp_path)

    async def _download(
        self,
        job: Job,
        url: str,
        filename: str,
        *,
        expected_size: int = 0,
    ) -> tuple[Path, int]:
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=20,
            sock_read=120,
        )
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"terabox-{job.job_id}-",
            suffix=Path(filename).suffix or ".bin",
            delete=False,
            dir="/tmp",
        )
        path = Path(tmp.name)
        total = 0
        last_log = 0

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "*/*",
                    },
                    allow_redirects=True,
                ) as response:
                    response.raise_for_status()
                    content_length = int(response.headers.get("Content-Length") or 0)
                    declared = max(expected_size, content_length)

                    log.info(
                        "job=%s downloading HTTP=%s size=%s",
                        job.job_id,
                        response.status,
                        declared,
                    )

                    await self._callback(
                        job,
                        "downloading",
                        f"Downloading {filename}.",
                        file_name=filename,
                        size=declared,
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
                                log.info(
                                    "job=%s download_progress=%s bytes",
                                    job.job_id,
                                    total,
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

        return path, total

    async def _send_file(self, chat_id: int, path: Path, filename: str):
        streaming_extensions = {
            ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".aac"
        }
        supports_streaming = path.suffix.lower() in streaming_extensions

        await self.telegram.send_file(
            chat_id,
            str(path),
            caption=filename,
            force_document=not supports_streaming,
            supports_streaming=supports_streaming,
        )

    @staticmethod
    def _safe_filename(value: str) -> str:
        clean = Path(value.replace("\\", "/")).name.strip()
        clean = "".join(
            char if char.isprintable() and char not in "\x00\r\n"
            else "_"
            for char in clean
        )
        return clean[:240] or "terabox-file"


app = FastAPI(title="TeraBox VPS Worker")
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
    return {
        "status": "ok",
        "queue_size": worker.queue.qsize(),
        "browser": bool(worker.resolver.browser),
        "telegram_connected": worker.telegram.is_connected(),
    }


@app.post("/job")
async def create_job(job: Job, x_worker_secret: str = Header(default="")):
    if not worker:
        raise HTTPException(503, "Worker is starting.")
    if x_worker_secret != worker.worker_secret:
        raise HTTPException(404)

    await worker.enqueue(job)
    return {
        "accepted": True,
        "job_id": job.job_id,
        "queue_size": worker.queue.qsize(),
    }
