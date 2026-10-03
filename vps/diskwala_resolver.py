from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.async_api import Browser, Page


DISKWALA_HOSTS = {
    "diskwala.com",
    "www.diskwala.com",
}

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mpeg", ".mpg", ".3gp", ".ts", ".flv",
    ".m3u8", ".mp3", ".wav", ".m4a", ".aac",
    ".pdf", ".zip", ".rar", ".7z",
}

DISKWALA_ID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])"
)
URL_RE = re.compile(r"https?://[^\s<>'\"\\]+", re.IGNORECASE)


def is_diskwala_url(value: str) -> bool:
    try:
        host = (urlparse(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in DISKWALA_HOSTS or host.endswith(".diskwala.com")


def extract_diskwala_id(value: str) -> str:
    match = DISKWALA_ID_RE.search(value or "")
    if not match:
        raise ValueError("Could not extract a Diskwala share id.")
    return match.group(0)


def _clean_url(value: str) -> str:
    return html.unescape(value or "").replace("\\/", "/").replace(
        "\\u0026", "&"
    ).strip(" \t\r\n\\\"'<>[]()")


def _is_bad_asset(url: str) -> bool:
    lower = url.lower()
    path = urlparse(url).path.lower()
    return (
        url.startswith(("blob:", "data:", "javascript:"))
        or any(
            path.endswith(ext)
            for ext in (
                ".js", ".css", ".map", ".png", ".jpg", ".jpeg",
                ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
            )
        )
        or "favicon" in lower
    )


def _is_hls(url: str, content_type: str = "") -> bool:
    lower_type = content_type.lower()
    return ".m3u8" in url.lower() or "mpegurl" in lower_type


def _is_media(url: str, content_type: str = "") -> bool:
    if _is_hls(url, content_type):
        return True

    path = urlparse(url).path.lower()
    content_type = content_type.lower()

    return (
        any(path.endswith(ext) for ext in MEDIA_EXTENSIONS)
        or content_type.startswith("video/")
        or content_type.startswith("audio/")
        or content_type in {"application/octet-stream", "binary/octet-stream"}
    )


def _score_candidate(url: str, content_type: str = "", *, source: str = "") -> int:
    if not url or _is_bad_asset(url):
        return -10000

    path = urlparse(url).path.lower()
    lower = url.lower()
    score = 0
    content_type = content_type.lower()

    if _is_hls(url, content_type):
        score += 190
    if any(path.endswith(ext) for ext in MEDIA_EXTENSIONS):
        score += 160
    if content_type.startswith("video/"):
        score += 150
    elif content_type.startswith("audio/"):
        score += 120
    elif content_type in {"application/octet-stream", "binary/octet-stream"}:
        score += 60

    if any(marker in path for marker in ("/download", "/file/", "/stream", "/media/")):
        score += 70
    if any(marker in lower for marker in ("cdn", "s3", "storage", "cloudfront")):
        score += 25
    if source == "video":
        score += 50
    if "diskwala.com" not in (urlparse(url).hostname or "").lower():
        score += 15

    return score


def _extract_url_strings(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 5:
        return []

    if isinstance(value, str):
        value = _clean_url(value)
        found = []
        if value.startswith(("http://", "https://")):
            found.append(value)
        found.extend(_clean_url(match) for match in URL_RE.findall(value))
        return found

    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            key_text = str(key).lower()
            if any(
                marker in key_text
                for marker in (
                    "url", "download", "stream", "source",
                    "file", "media", "video", "audio",
                )
            ):
                found.extend(_extract_url_strings(item, depth=depth + 1))
            elif depth < 2:
                found.extend(_extract_url_strings(item, depth=depth + 1))
        return found

    if isinstance(value, list):
        found: list[str] = []
        for item in value[:50]:
            found.extend(_extract_url_strings(item, depth=depth + 1))
        return found

    return []


def _first_filename(data: Any, *, depth: int = 0) -> str:
    if depth > 5:
        return ""

    if isinstance(data, dict):
        for key in (
            "filename", "fileName", "name", "originalName",
            "original_name", "server_filename", "title",
        ):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return Path(value.replace("\\", "/")).name.strip()

        for value in data.values():
            result = _first_filename(value, depth=depth + 1)
            if result:
                return result

    elif isinstance(data, list):
        for item in data[:25]:
            result = _first_filename(item, depth=depth + 1)
            if result:
                return result

    return ""


def _first_size(data: Any, *, depth: int = 0) -> int:
    if depth > 5:
        return 0

    if isinstance(data, dict):
        for key in (
            "size", "sizeBytes", "size_bytes",
            "filesize", "fileSize", "contentLength",
        ):
            try:
                number = int(data.get(key) or 0)
            except (TypeError, ValueError):
                number = 0
            if number > 0:
                return number

        for value in data.values():
            result = _first_size(value, depth=depth + 1)
            if result:
                return result

    elif isinstance(data, list):
        for item in data[:25]:
            result = _first_size(item, depth=depth + 1)
            if result:
                return result

    return 0


def _json_values_from_text(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []

    for match in re.finditer(r"[\{\[]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except Exception:
            continue
        values.append(value)
        if len(values) >= 20:
            break

    return values


class DiskwalaBrowserResolver:
    """Resolve public Diskwala share pages without a paid API."""

    def __init__(self, *, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self.browser: Browser | None = None
        self._semaphore: asyncio.Semaphore | None = None

    async def start(self, browser: Browser | None):
        if not browser:
            raise RuntimeError("Shared Chromium browser is not started.")
        self.browser = browser
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def stop(self):
        self.browser = None
        self._semaphore = None

    async def resolve(
        self,
        share_url: str,
        *,
        allow_native_download: bool = True,
    ) -> dict[str, Any]:
        if not is_diskwala_url(share_url):
            raise ValueError("Unsupported Diskwala URL.")
        if not self.browser or not self._semaphore:
            raise RuntimeError("Diskwala resolver is not started.")

        started = time.monotonic()

        async with self._semaphore:
            context = await self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = await context.new_page()

            response_records: list[dict[str, Any]] = []
            request_urls: list[str] = []
            response_bodies: list[str] = []

            async def capture_response(response):
                record = {
                    "url": _clean_url(response.url),
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                    "content_length": response.headers.get("content-length", ""),
                    "content_disposition": response.headers.get(
                        "content-disposition", ""
                    ),
                }
                response_records.append(record)

                content_type = record["content_type"].lower()
                if (
                    "json" in content_type
                    or "javascript" in content_type
                    or "text/plain" in content_type
                ):
                    try:
                        body = await response.text()
                    except Exception:
                        return
                    if body and len(body) <= 500000:
                        response_bodies.append(body)

            def capture_request(request):
                url = _clean_url(request.url)
                if url and url not in request_urls:
                    request_urls.append(url)

            page.on("response", capture_response)
            page.on("request", capture_request)

            try:
                response = await page.goto(
                    share_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                if response:
                    print(
                        f"Diskwala page status={response.status} final={page.url}",
                        flush=True,
                    )

                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # Give the app/player JavaScript time to populate metadata and
                # request its media URL.
                await page.wait_for_timeout(2500)

                try:
                    await page.locator("video, audio").first.evaluate(
                        "(el) => { el.muted = true; return el.play().catch(() => null); }"
                    )
                    await page.wait_for_timeout(1200)
                except Exception:
                    pass

                dom_data = await page.evaluate(
                    """
                    () => ({
                      media: Array.from(document.querySelectorAll('video, audio')).map((el) => ({
                        src: el.currentSrc || el.src || '',
                        poster: el.poster || '',
                        title: el.getAttribute('title') || '',
                        aria: el.getAttribute('aria-label') || ''
                      })),
                      sources: Array.from(document.querySelectorAll('video source, audio source'))
                        .map((el) => el.src || el.getAttribute('src') || ''),
                      anchors: Array.from(document.querySelectorAll(
                        'a[href], [data-url], [data-download-url], [data-src]'
                      )).map((el) => ({
                        href: el.href || '',
                        url: el.getAttribute('data-url')
                          || el.getAttribute('data-download-url')
                          || el.getAttribute('data-src')
                          || '',
                        text: (el.innerText || '').trim().slice(0, 120)
                      })),
                      metas: Array.from(document.querySelectorAll('meta')).map((el) => ({
                        property: el.getAttribute('property') || '',
                        name: el.getAttribute('name') || '',
                        content: el.getAttribute('content') || ''
                      })),
                      title: document.title || '',
                      bodyText: (document.body?.innerText || '').slice(0, 2500)
                    })
                    """
                )

                html_text = await page.content()
                normalized_html = _clean_url(html_text)
                candidates: dict[str, dict[str, Any]] = {}

                def add_candidate(
                    url: str,
                    *,
                    content_type: str = "",
                    size: int = 0,
                    filename: str = "",
                    source: str = "",
                ):
                    url = _clean_url(url)
                    if not url.startswith(("http://", "https://")) or _is_bad_asset(url):
                        return

                    current = candidates.get(url)
                    if current is None:
                        candidates[url] = {
                            "url": url,
                            "content_type": content_type,
                            "size": int(size or 0),
                            "filename": filename,
                            "source": source,
                        }
                        return

                    if content_type and not current["content_type"]:
                        current["content_type"] = content_type
                    if size and not current["size"]:
                        current["size"] = int(size)
                    if filename and not current["filename"]:
                        current["filename"] = filename

                for item in dom_data.get("media", []):
                    add_candidate(
                        item.get("src") or "",
                        source="video",
                        filename=item.get("title") or item.get("aria") or "",
                    )
                    add_candidate(item.get("poster") or "", source="poster")

                for value in dom_data.get("sources", []):
                    add_candidate(value, source="source")

                for item in dom_data.get("anchors", []):
                    text_value = str(item.get("text") or "")
                    add_candidate(item.get("href") or "", source="anchor")
                    add_candidate(item.get("url") or "", source="data-attribute")
                    if "download" in text_value.lower():
                        add_candidate(item.get("href") or "", source="download-anchor")

                for item in response_records:
                    content_length = str(item.get("content_length") or "")
                    try:
                        size = int(content_length)
                    except ValueError:
                        size = 0

                    add_candidate(
                        item["url"],
                        content_type=item["content_type"],
                        size=size,
                        source="response",
                    )

                    disposition = str(item.get("content_disposition") or "")
                    match = re.search(
                        r"filename\*?=(?:UTF-8'')?[\"']?([^;\"']+)",
                        disposition,
                        re.IGNORECASE,
                    )
                    if match and item["url"] in candidates:
                        candidates[item["url"]]["filename"] = unquote(
                            match.group(1)
                        )

                for url in request_urls[-300:]:
                    add_candidate(url, source="request")

                try:
                    performance_urls = await page.evaluate(
                        "() => performance.getEntriesByType('resource').map(x => x.name)"
                    )
                except Exception:
                    performance_urls = []

                for url in performance_urls[-300:]:
                    add_candidate(url, source="performance")

                # Diskwala may keep the CDN URL in a JS bootstrap object rather
                # than putting it directly on the video element.
                for match in URL_RE.findall(normalized_html):
                    add_candidate(match, source="html")

                for body in response_bodies[-100:]:
                    for match in URL_RE.findall(_clean_url(body)):
                        add_candidate(match, source="payload")

                    for parsed in _json_values_from_text(body):
                        for value in _extract_url_strings(parsed):
                            add_candidate(value, source="json")

                        json_name = _first_filename(parsed)
                        json_size = _first_size(parsed)
                        if json_name or json_size:
                            for candidate in candidates.values():
                                if json_name and not candidate["filename"]:
                                    candidate["filename"] = json_name
                                if json_size and not candidate["size"]:
                                    candidate["size"] = json_size

                direct = None
                stream = None

                scored = sorted(
                    candidates.values(),
                    key=lambda item: _score_candidate(
                        item["url"],
                        item["content_type"],
                        source=item["source"],
                    ),
                    reverse=True,
                )

                for candidate in scored:
                    score = _score_candidate(
                        candidate["url"],
                        candidate["content_type"],
                        source=candidate["source"],
                    )
                    if score < 40:
                        continue

                    if _is_hls(candidate["url"], candidate["content_type"]):
                        if stream is None:
                            stream = candidate
                    elif _is_media(candidate["url"], candidate["content_type"]):
                        if direct is None:
                            direct = candidate

                browser_download_path = ""
                browser_filename = ""

                if not direct and not stream and allow_native_download:
                    browser_download_path, browser_filename = (
                        await self._browser_native_download(
                            page,
                            fallback_filename=(
                                _first_filename_from_dom(dom_data)
                                or dom_data.get("title")
                                or "diskwala-file"
                            ),
                        )
                    )

                share_id = extract_diskwala_id(share_url)

                cookies = "; ".join(
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in await page.context.cookies()
                )

                if browser_download_path:
                    size = os.path.getsize(browser_download_path)
                    filename = browser_filename or Path(browser_download_path).name
                    elapsed = round((time.monotonic() - started) * 1000)
                    return self._build_result(
                        share_url=share_url,
                        page=page,
                        filename=filename,
                        size=size,
                        direct_url="",
                        stream_url="",
                        browser_download_path=browser_download_path,
                        download_mode="browser",
                        share_id=share_id,
                        cookies=cookies,
                        elapsed=elapsed,
                    )

                selected = direct or stream
                if not selected:
                    diagnostics = {
                        "title": dom_data.get("title", ""),
                        "final_url": page.url,
                        "candidate_count": len(candidates),
                        "top_candidates": [
                            {
                                "url": item["url"].split("?", 1)[0][:240],
                                "content_type": item["content_type"],
                                "source": item["source"],
                                "score": _score_candidate(
                                    item["url"],
                                    item["content_type"],
                                    source=item["source"],
                                ),
                            }
                            for item in scored[:20]
                        ],
                        "body": str(dom_data.get("bodyText") or "")[:800],
                    }
                    print(
                        "Diskwala resolver diagnostics="
                        + json.dumps(diagnostics, ensure_ascii=False)[:12000],
                        flush=True,
                    )
                    raise RuntimeError(
                        "Diskwala page loaded, but no direct media URL was exposed. "
                        "The public link may have download protection or an unsupported page layout."
                    )

                filename = (
                    selected.get("filename")
                    or _filename_from_url(selected["url"])
                    or _first_filename_from_dom(dom_data)
                    or "diskwala-file"
                ).strip()

                if "." not in Path(filename).name and _is_hls(
                    selected["url"], selected.get("content_type", "")
                ):
                    filename += ".mp4"

                elapsed = round((time.monotonic() - started) * 1000)
                return self._build_result(
                    share_url=share_url,
                    page=page,
                    filename=filename,
                    size=int(selected.get("size") or 0),
                    direct_url=selected["url"] if direct else "",
                    stream_url=selected["url"] if stream else "",
                    browser_download_path="",
                    download_mode="direct" if direct else "stream",
                    share_id=share_id,
                    cookies=cookies,
                    elapsed=elapsed,
                )
            finally:
                try:
                    page.remove_listener("response", capture_response)
                    page.remove_listener("request", capture_request)
                except Exception:
                    pass
                await context.close()

    async def _browser_native_download(
        self,
        page: Page,
        *,
        fallback_filename: str,
    ) -> tuple[str, str]:
        holder: dict[str, Any] = {}

        async def on_download(download):
            if "download" not in holder:
                holder["download"] = download

        page.on("download", on_download)

        try:
            selectors = (
                "a[download]:visible",
                "button:has-text('Download'):visible",
                "a:has-text('Download'):visible",
                "[data-action*='download']:visible",
                "[data-download]:visible",
                "[class*='download']:visible",
            )

            button = None
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    count = min(await locator.count(), 8)
                    for index in range(count):
                        candidate = locator.nth(index)
                        if not await candidate.is_visible():
                            continue

                        text_value = (
                            await candidate.inner_text()
                        ).strip().lower()
                        href = (
                            await candidate.get_attribute("href") or ""
                        ).lower()
                        data_url = (
                            await candidate.get_attribute("data-download-url")
                            or ""
                        ).lower()

                        if (
                            "download" in text_value
                            or "download" in href
                            or "download" in data_url
                            or selector.startswith("a[download]")
                        ):
                            button = candidate
                            break

                    if button:
                        break
                except Exception:
                    continue

            if not button:
                return "", ""

            await button.scroll_into_view_if_needed()
            print("Diskwala resolver: clicking public Download control", flush=True)
            await button.click(timeout=30000, force=True)

            for _ in range(60):
                download = holder.get("download")
                if download:
                    suggested = (
                        str(
                            getattr(download, "suggested_filename", "")
                            or ""
                        ).strip()
                        or fallback_filename
                        or "diskwala-file"
                    )

                    target_dir = Path(
                        os.getenv(
                            "DISKWALA_DOWNLOAD_DIR",
                            os.getenv(
                                "DOWNLOADS_DIR",
                                "/tmp/terabox-downloads",
                            ),
                        )
                    )
                    target_dir.mkdir(parents=True, exist_ok=True)

                    suffix = Path(suggested).suffix or ".bin"
                    fd, temp_name = tempfile.mkstemp(
                        prefix="diskwala-browser-",
                        suffix=suffix,
                        dir=str(target_dir),
                    )
                    os.close(fd)
                    await download.save_as(temp_name)
                    return temp_name, suggested

                await page.wait_for_timeout(500)

            return "", ""
        except Exception as exc:
            print(
                f"Diskwala browser download fallback failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return "", ""
        finally:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass

    @staticmethod
    def _build_result(
        *,
        share_url: str,
        page: Page,
        filename: str,
        size: int,
        direct_url: str,
        stream_url: str,
        browser_download_path: str,
        download_mode: str,
        share_id: str,
        cookies: str,
        elapsed: int,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "file_name": filename,
            "size": int(size or 0),
            "fs_id": share_id,
            "direct_url": direct_url,
            "stream_url": stream_url,
            "browser_download_path": browser_download_path,
            "download_mode": download_mode,
            "thumbnail": "",
            "sign": "",
            "timestamp": "",
            "share_id": share_id,
            "uk": "",
            "duration": 0,
            "cookies": cookies,
            "referer_url": page.url,
            "files": [{
                "file_name": filename,
                "size": int(size or 0),
                "fs_id": share_id,
                "direct_url": direct_url,
                "stream_url": stream_url,
                "browser_download_path": browser_download_path,
                "is_dir": False,
            }],
            "surl": share_url,
            "resolve_ms": elapsed,
        }


def _filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name.strip()
    return name if name and "." in name else ""


def _first_filename_from_dom(dom_data: dict[str, Any]) -> str:
    for item in dom_data.get("media", []):
        for value in (item.get("title"), item.get("aria")):
            if value and str(value).strip():
                return Path(str(value).replace("\\", "/")).name.strip()

    for item in dom_data.get("metas", []):
        prop = str(item.get("property") or "").lower()
        name = str(item.get("name") or "").lower()
        if prop in {"og:title", "og:filename"} or name in {"title", "filename"}:
            value = str(item.get("content") or "").strip()
            if value:
                return Path(value.replace("\\", "/")).name.strip()

    title = str(dom_data.get("title") or "").strip()
    if title and "diskwala" not in title.lower():
        return Path(title.replace("\\", "/")).name.strip()

    return ""
