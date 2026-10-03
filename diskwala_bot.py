import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page, Route
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PORT = int(os.getenv("PORT", "18080"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(10 * 1024**3)))
RESOLVE_TIMEOUT_SECONDS = int(os.getenv("RESOLVE_TIMEOUT_SECONDS", "35"))
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "1800"))
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip()
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s diskwala-bot %(message)s",
)
log = logging.getLogger("diskwala-bot")

DISKWALA_API_HOST = "ddudapidd.diskwala.com"
DISKWALA_API_PREFIX = f"https://{DISKWALA_API_HOST}/api/v1/"
DISKWALA_ORIGIN = "https://www.diskwala.com"

URL_RE = re.compile(r"https?://(?:www\.)?diskwala\.com/(?:app|sharing)/[^\s<>]+", re.I)

URL_KEYS = {
    "download_url": 100, "downloadurl": 100, "direct_link": 100, "directlink": 100,
    "signed_url": 95, "signedurl": 95, "file_url": 90, "fileurl": 90,
    "url": 80, "link": 70, "m3u8_url": 65, "m3u8url": 65,
    "stream_url": 55, "streamurl": 55,
}
NAME_KEYS = ("file_name", "filename", "original_name", "originalname", "name", "title")
SIZE_KEYS = ("sizebytes", "size_bytes", "filesize", "file_size", "size")


@dataclass
class ResolvedFile:
    url: str
    filename: str
    size: int | None = None
    mime: str | None = None
    is_hls: bool = False


def normalize_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip("\"'")
    if not value.startswith(("http://", "https://")):
        return None
    host = urlparse(value).netloc.lower()
    if host in {"www.diskwala.com", "diskwala.com", DISKWALA_API_HOST}:
        return None
    return value


def walk_objects(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        yield value, path
        for k, v in value.items():
            yield from walk_objects(v, path + (str(k),))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk_objects(v, path + (str(i),))


def extract_candidates(payload: Any) -> list[tuple[int, str, tuple[str, ...]]]:
    found = []
    for obj, path in walk_objects(payload):
        for key, value in obj.items():
            if not isinstance(value, str):
                continue
            score = URL_KEYS.get(str(key).lower().replace("-", "_"), 0)
            url = normalize_url(value)
            if not url:
                continue
            low = url.lower()
            if "m3u8" in low:
                score += 10
            if any(x in low for x in (".mp4", ".mkv", ".mov", ".webm", ".zip", ".pdf", ".mp3", ".m4a")):
                score += 15
            found.append((score, url, path + (str(key),)))
    return sorted(found, reverse=True)


def extract_name(payload: Any) -> str | None:
    for obj, _ in walk_objects(payload):
        for key in NAME_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value.strip()).name
    return None


def extract_size(payload: Any) -> int | None:
    for obj, _ in walk_objects(payload):
        for key in SIZE_KEYS:
            value = obj.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
            if isinstance(value, str):
                digits = re.sub(r"[^0-9]", "", value)
                if digits:
                    try:
                        return int(digits)
                    except ValueError:
                        pass
    return None


def safe_filename(name: str | None, fallback: str = "diskwala_file") -> str:
    name = (name or fallback).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)
    name = name[:180].strip(" .")
    return name or fallback


def parse_json_body(body: bytes | str | None) -> Any | None:
    if not body:
        return None
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return json.loads(body)
    except Exception:
        return None


class DiskwalaResolver:
    def __init__(self):
        self.playwright = None
        self.browser = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        log.info("DiskWala Chromium resolver started")

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _proxy_api(self, route: Route, api_context):
        request = route.request
        url = request.url
        headers = await request.all_headers()
        for key in ("host", "content-length", "connection", "accept-encoding"):
            headers.pop(key, None)

        try:
            response = await api_context.fetch(
                url,
                method=request.method,
                headers=headers,
                data=request.post_data_buffer if request.post_data_buffer is not None else None,
                timeout=30000,
                fail_on_status_code=False,
            )
            response_headers = dict(response.headers)
            response_headers.pop("content-encoding", None)
            response_headers.pop("content-length", None)
            response_headers["access-control-allow-origin"] = DISKWALA_ORIGIN
            response_headers["access-control-allow-credentials"] = "true"
            response_body = await response.body()

            # Safe diagnostics: never log cookies, authorization, Appicrypt values, or
            # request bodies. These fields are enough to see why the API rejects us.
            request_header_names = sorted(
                k.lower() for k in headers.keys()
                if k.lower() not in {"cookie", "authorization", "appicrypt", "appicrypt-ts"}
            )
            response_content_type = response.headers.get("content-type", "")
            safe_body = ""
            if response.status >= 400:
                decoded = response_body.decode("utf-8", "replace").strip()
                if len(decoded) <= 200 and not any(
                    marker in decoded.lower()
                    for marker in ("token", "cookie", "appicrypt", "authorization", "secret")
                ):
                    safe_body = decoded

            log.info(
                "proxied DiskWala API %s %s -> %s bytes=%d content_type=%s",
                request.method, url, response.status, len(response_body), response_content_type,
            )
            log.info(
                "DiskWala API request diagnostics method=%s path=%s "
                "origin=%s referer=%s header_names=%s",
                request.method,
                urlparse(url).path,
                headers.get("origin", ""),
                headers.get("referer", ""),
                request_header_names,
            )
            if response.status >= 400:
                log.warning(
                    "DiskWala API error diagnostics status=%s content_type=%s body=%r",
                    response.status,
                    response_content_type,
                    safe_body,
                )
            await route.fulfill(
                status=response.status,
                headers=response_headers,
                body=response_body,
            )
        except Exception as exc:
            log.warning("DiskWala API proxy failed %s %s: %s", request.method, url, exc)
            try:
                await route.continue_()
            except Exception:
                await route.abort()

    async def resolve(self, share_url: str) -> ResolvedFile:
        context = await self.browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        api_payloads: list[tuple[str, Any]] = []
        media_urls: list[str] = []
        downloads = []

        async def on_response(response):
            if DISKWALA_API_HOST in response.url and "/api/v1/" in response.url:
                try:
                    body = await response.body()
                    payload = parse_json_body(body)
                    if payload is not None:
                        api_payloads.append((response.url, payload))
                        for _, url, _ in extract_candidates(payload):
                            if url not in media_urls:
                                media_urls.append(url)
                                log.info("DiskWala candidate media URL discovered: %s", url)
                except Exception as exc:
                    log.debug("API response inspection failed: %s", exc)

            content_type = (response.headers.get("content-type") or "").lower()
            if response.request.resource_type in {"media", "xhr", "fetch"} and (
                "video/" in content_type
                or "audio/" in content_type
                or "application/octet-stream" in content_type
            ):
                candidate = normalize_url(response.url)
                if candidate and candidate not in media_urls:
                    media_urls.append(candidate)
                    log.info("DiskWala media response discovered: %s", candidate)

        async def on_request(request):
            if DISKWALA_API_HOST in request.url and "/api/v1/" in request.url:
                try:
                    headers = await request.all_headers()
                    names = sorted(k.lower() for k in headers)
                    special = {
                        "appicrypt": "appicrypt" in {k.lower() for k in headers},
                        "appicrypt_ts": "appicrypt-ts" in {k.lower() for k in headers},
                        "cookie": "cookie" in {k.lower() for k in headers},
                        "authorization": "authorization" in {k.lower() for k in headers},
                    }
                    log.info(
                        "DiskWala official API request method=%s path=%s "
                        "special_headers=%s header_names=%s",
                        request.method,
                        urlparse(request.url).path,
                        special,
                        names,
                    )
                except Exception as exc:
                    log.debug("DiskWala request diagnostics failed: %s", exc)

        async def on_request_failed(request):
            if DISKWALA_API_HOST in request.url:
                log.warning(
                    "DiskWala API browser request failed: %s %s failure=%s",
                    request.method, request.url, request.failure,
                )

        async def on_download(download):
            downloads.append(download)

        try:
            page: Page = await context.new_page()
            page.on("response", on_response)
            page.on("request", on_request)
            page.on("requestfailed", on_request_failed)
            page.on("download", on_download)

            log.info("Resolving DiskWala share: %s", share_url)
            await page.goto(share_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(15000)

            controls = await page.locator("a,button").all()
            for control in controls:
                try:
                    text = (await control.inner_text()).strip().lower()
                    aria = (await control.get_attribute("aria-label") or "").lower()
                    href = await control.get_attribute("href")
                    if "download" in text or "download" in aria:
                        if href:
                            candidate = normalize_url(href)
                            if candidate:
                                media_urls.append(candidate)
                                break
                        try:
                            await control.click(timeout=3000)
                            await page.wait_for_timeout(3000)
                            break
                        except Exception:
                            pass
                except Exception:
                    continue

            if downloads:
                path = await downloads[0].path()
                if path:
                    filename = safe_filename(await downloads[0].suggested_filename())
                    return ResolvedFile(str(path), filename)

            if not media_urls:
                for _, payload in api_payloads:
                    for _, url, _ in extract_candidates(payload):
                        if url not in media_urls:
                            media_urls.append(url)

            if not media_urls:
                raise RuntimeError(
                    "DiskWala did not expose a media URL after the official client requests were proxied."
                )

            media_urls.sort(key=lambda u: ("m3u8" in u.lower(), len(u)))
            url = media_urls[0]
            filename = None
            size = None
            for _, payload in api_payloads:
                filename = filename or extract_name(payload)
                size = size or extract_size(payload)

            if not filename:
                filename = Path(urlparse(url).path).name or "diskwala_file"

            return ResolvedFile(
                url=url,
                filename=safe_filename(filename),
                size=size,
                mime=mimetypes.guess_type(filename)[0],
                is_hls=".m3u8" in url.lower(),
            )
        finally:
            await context.close()


async def download_resolved(resolved: ResolvedFile) -> tuple[str, int]:
    if os.path.isfile(resolved.url):
        size = os.path.getsize(resolved.url)
        if size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"File is too large: {size} bytes")
        return resolved.url, size

    workdir = Path(tempfile.mkdtemp(prefix="diskwala-"))
    output = workdir / safe_filename(resolved.filename)

    if resolved.is_hls:
        final = output if output.suffix.lower() in {".mp4", ".mkv", ".ts"} else output.with_suffix(".mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "warning", "-i", resolved.url, "-c", "copy", str(final)]
        log.info("Downloading DiskWala HLS with ffmpeg")
        proc = await asyncio.create_subprocess_exec(*cmd)
        try:
            await asyncio.wait_for(proc.wait(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("HLS download timed out")
        if proc.returncode != 0 or not final.exists():
            raise RuntimeError("ffmpeg could not download the DiskWala stream")
        size = final.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"File is too large: {size} bytes")
        return str(final), size

    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": DISKWALA_ORIGIN + "/",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(resolved.url, allow_redirects=True) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(f"File is too large: {content_length} bytes")
            total = 0
            with output.open("wb") as fp:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("File exceeded MAX_DOWNLOAD_BYTES")
                    fp.write(chunk)
    return str(output), total


class DiskwalaBot:
    def __init__(self):
        self.client = TelegramClient(MemorySession(), API_ID, API_HASH)
        self.resolver = DiskwalaResolver()

    async def start(self):
        await self.resolver.start()
        await self.client.start(bot_token=BOT_TOKEN)
        me = await self.client.get_me()
        log.info("Telegram bot connected: username=@%s id=%s", me.username, me.id)

        @self.client.on(events.NewMessage(incoming=True))
        async def handler(event):
            if event.chat_id is not None and ALLOWED_CHAT_IDS and event.chat_id not in ALLOWED_CHAT_IDS:
                return
            text = (event.raw_text or "").strip()
            match = URL_RE.search(text)
            if not match:
                return

            share_url = match.group(0).rstrip(".,);]")
            task = f"{event.id}-{int(time.time())}"
            log.info("task=%s accepted DiskWala URL=%s chat_id=%s", task, share_url, event.chat_id)
            status = await event.reply("🔎 Resolving DiskWala link…")
            file_path = None
            try:
                resolved = await asyncio.wait_for(
                    self.resolver.resolve(share_url),
                    timeout=RESOLVE_TIMEOUT_SECONDS + 20,
                )
                await status.edit(f"📄 {resolved.filename}\n⬇️ Downloading…")
                file_path, size = await download_resolved(resolved)
                await status.edit(f"📤 Uploading {resolved.filename} ({size / 1024**2:.1f} MB)…")
                await self.client.send_file(
                    event.chat_id,
                    file_path,
                    caption=f"✅ {resolved.filename}",
                    force_document=True,
                )
                await status.delete()
                log.info("task=%s completed filename=%s bytes=%s", task, resolved.filename, size)
            except Exception as exc:
                log.exception("task=%s failed", task)
                await status.edit(f"❌ DiskWala download failed.\n{type(exc).__name__}: {exc}")
            finally:
                if file_path and os.path.isfile(file_path):
                    shutil.rmtree(str(Path(file_path).parent), ignore_errors=True)

        await self.client.run_until_disconnected()

    async def stop(self):
        await self.resolver.close()
        await self.client.disconnect()


async def health(_request):
    return web.json_response({"status": "ok", "service": "diskwala-downloader"})


async def main():
    bot = DiskwalaBot()
    app = web.Application()
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    try:
        await bot.start()
    finally:
        await bot.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
