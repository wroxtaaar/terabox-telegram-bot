from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.async_api import Browser, Page, async_playwright


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
    "teraboxshare.com",
    "www.teraboxshare.com",
}

APP_ID = "250528"

STREAM_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
    ".mpeg", ".mpg", ".3gp", ".ts", ".flv",
}


def is_terabox_url(value: str) -> bool:
    try:
        host = (urlparse(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in TERABOX_HOSTS or host.endswith(".terabox.com")


def extract_surl(value: str) -> str:
    parsed = urlparse(value.strip())
    query = parse_qs(parsed.query)

    if query.get("surl"):
        raw = query["surl"][0].strip()
    else:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
        if not match:
            raise ValueError("Could not extract a TeraBox share code.")
        raw = match.group(1)

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", raw):
        raise ValueError("Invalid TeraBox share code.")

    return raw


def _balanced_object(text: str, marker: str) -> dict[str, Any] | None:
    start = text.find(marker)
    if start < 0:
        return None

    start = text.find("{", start)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None

    return None


def _find_jstoken(html: str, browser_data: dict[str, Any]) -> str:
    for key in ("jsToken", "jstoken", "js_token"):
        value = browser_data.get(key)
        if value:
            return str(value)

    patterns = (
        r"fn%28%22([0-9a-fA-F]+)%22%29",
        r"fn\\(\\x22([^\\x22]+)\\x22\\)",
        r'["\\\']jsToken["\\\']\\s*[:=]\\s*["\\\']([^"\\\']+)',
        r'window\\.jsToken\\s*=\\s*["\\\']([^"\\\']+)',
        r'jsToken\\s*[:=]\\s*["\\\']([^"\\\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    return ""


def _normalize_file(row: dict[str, Any]) -> dict[str, Any]:
    thumbs = row.get("thumbs") if isinstance(row.get("thumbs"), dict) else {}
    try:
        size = int(row.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    direct_url = str(
        row.get("dlink")
        or row.get("download_url")
        or row.get("direct_url")
        or ""
    ).strip()

    thumb_url = str(
        thumbs.get("url3")
        or thumbs.get("url2")
        or thumbs.get("url1")
        or row.get("thumbnail")
        or row.get("thumb")
        or ""
    ).strip()

    sign = str(row.get("sign") or "").strip()
    timestamp = str(row.get("timestamp") or "").strip()

    if thumb_url and (not sign or not timestamp):
        try:
            thumb_query = parse_qs(urlparse(thumb_url).query)
            sign = sign or str(thumb_query.get("sign", [""])[0]).strip()
            timestamp = timestamp or str(
                (thumb_query.get("time") or thumb_query.get("timestamp") or [""])[0]
            ).strip()
        except Exception:
            pass

    try:
        duration = int(row.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0

    return {
        "file_name": str(
            row.get("server_filename")
            or row.get("filename")
            or row.get("name")
            or ""
        ).strip(),
        "size": size,
        "fs_id": str(row.get("fs_id") or row.get("fid") or "").strip(),
        "path": str(row.get("path") or "").strip(),
        "is_dir": str(row.get("isdir") or row.get("is_dir") or "0").lower()
        in {"1", "true"},
        "direct_url": direct_url,
        "stream_url": str(row.get("stream_url") or "").strip(),
        "thumbnail": thumb_url,
        "sign": sign,
        "timestamp": timestamp,
        "duration": duration,
    }


def _build_stream_url(
    *,
    share_id: str,
    uk: str,
    fs_id: str,
    sign: str,
    timestamp: str,
) -> str:
    if not all((share_id, uk, fs_id, sign, timestamp)):
        return ""

    return (
        "https://www.terabox.app/share/streaming?"
        f"app_id={APP_ID}&web=1&channel=dubox&clienttype=0"
        f"&shareid={quote_plus(share_id)}"
        f"&uk={quote_plus(uk)}"
        f"&fid={quote_plus(fs_id)}"
        f"&sign={quote_plus(sign)}"
        f"&timestamp={quote_plus(timestamp)}"
        "&type=M3U8_AUTO_480"
    )



def _rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    candidates = [
        data.get("list"),
        data.get("file_list"),
        data.get("FILE_LIST"),
    ]

    nested = data.get("data")
    if isinstance(nested, dict):
        candidates.extend(
            [nested.get("list"), nested.get("file_list"), nested.get("FILE_LIST")]
        )

    for candidate in candidates:
        if isinstance(candidate, list):
            return [_normalize_file(x) for x in candidate if isinstance(x, dict)]

    return []


class TeraBoxBrowserResolver:
    def __init__(self, *, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self._playwright = None
        self.browser: Browser | None = None
        self._semaphore = None

    async def start(self):
        if self.browser:
            return

        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
            ],
        )

        import asyncio

        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def stop(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def resolve(self, share_url: str) -> dict[str, Any]:
        if not is_terabox_url(share_url):
            raise ValueError("Unsupported TeraBox URL.")

        if not self.browser or not self._semaphore:
            raise RuntimeError("Browser resolver is not started.")

        surl = extract_surl(share_url)
        candidates = [surl]
        if not surl.startswith("1"):
            candidates.insert(0, "1" + surl)

        started = time.monotonic()

        async with self._semaphore:
            context = await self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            page = await context.new_page()

            try:
                last_error = None

                for candidate in candidates:
                    try:
                        result = await self._resolve_candidate(
                            page, share_url, candidate
                        )
                        elapsed = (time.monotonic() - started) * 1000
                        result["resolve_ms"] = round(elapsed)
                        return result
                    except Exception as exc:
                        last_error = exc

                raise RuntimeError(
                    f"TeraBox Chromium resolver failed: {last_error}"
                )
            finally:
                await context.close()

    async def _resolve_candidate(
        self,
        page: Page,
        original_url: str,
        surl: str,
    ) -> dict[str, Any]:
        share_page = (
            "https://www.terabox.app/sharing/link?surl=" + quote_plus(surl)
        )

        print(f"resolve start surl={surl} page={share_page}", flush=True)

        responses: list[dict[str, Any]] = []
        response_payloads: list[dict[str, Any]] = []

        async def capture(response):
            url = response.url
            if any(
                marker in url
                for marker in (
                    "/share/list",
                    "/share/download",
                    "/api/shorturlinfo",
                )
            ):
                item = {
                    "url": url,
                    "status": response.status,
                    "content_type": response.headers.get("content-type", ""),
                }
                responses.append(item)
                try:
                    if "json" in item["content_type"].lower():
                        response_payloads.append(
                            {
                                **item,
                                "body": (await response.text())[:30000],
                            }
                        )
                except Exception:
                    pass

        page.on("response", capture)

        try:
            response = await page.goto(
                share_page,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if response:
                print(
                    f"share page status={response.status} final={page.url}",
                    flush=True,
                )

            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            # Give the site's JS a little time to populate yunData/jsToken.
            await page.wait_for_timeout(1800)

            browser_data = await page.evaluate(
                """
                () => {
                  const safe = (value) => {
                    try { return JSON.parse(JSON.stringify(value)); }
                    catch (_) { return null; }
                  };
                  let yun = null;
                  try {
                    if (typeof yunData !== 'undefined') yun = yunData;
                  } catch (_) {}
                  if (!yun) {
                    try { yun = window.yunData || null; } catch (_) {}
                  }

                  let token = '';
                  try { token = String(window.jsToken || ''); } catch (_) {}

                  return {
                    jsToken: token,
                    yunData: safe(yun),
                    href: location.href,
                    title: document.title,
                    cookies: document.cookie,
                  };
                }
                """
            )

            html = await page.content()
            js_token = _find_jstoken(html, browser_data)

            yun_data = browser_data.get("yunData")
            if not isinstance(yun_data, dict):
                yun_data = _balanced_object(html, "yunData")

            print(
                f"browser token={'yes' if js_token else 'no'} "
                f"yunData={'yes' if isinstance(yun_data, dict) else 'no'}",
                flush=True,
            )

            file_rows = []
            share_id = ""
            uk = ""
            sign = ""
            timestamp = ""
            dp_logid = ""
            randsk = ""

            if isinstance(yun_data, dict):
                meta = yun_data.get("SHARE_DATA")
                if not isinstance(meta, dict):
                    meta = yun_data

                share_id = str(
                    meta.get("SHARE_ID")
                    or meta.get("shareid")
                    or meta.get("share_id")
                    or ""
                )
                uk = str(meta.get("SHARE_UK") or meta.get("uk") or "")
                sign = str(meta.get("SIGN") or meta.get("sign") or "")
                timestamp = str(
                    meta.get("TIMESTAMP")
                    or meta.get("timestamp")
                    or ""
                )
                randsk = str(
                    meta.get("RANDSK")
                    or meta.get("randsk")
                    or ""
                )

                raw_files = (
                    yun_data.get("FILE_LIST")
                    or yun_data.get("file_list")
                    or yun_data.get("list")
                    or []
                )
                if isinstance(raw_files, list):
                    file_rows = [
                        _normalize_file(x)
                        for x in raw_files
                        if isinstance(x, dict)
                    ]

            if js_token:
                api_rows, api_meta = await self._browser_share_list(
                    page, surl, js_token, dp_logid
                )
                if api_rows:
                    file_rows = api_rows
                share_id = share_id or api_meta.get("shareid", "")
                uk = uk or api_meta.get("uk", "")
                sign = sign or api_meta.get("sign", "")
                timestamp = timestamp or api_meta.get("timestamp", "")

                # The page's own JavaScript may call share/list with extra
                # challenge parameters (pcftoken, psign, clientfrom, etc.).
                # Give those response listeners time to finish and prefer the
                # successful captured response over a simplified API call.
                await page.wait_for_timeout(500)
                captured_rows, captured_meta = self._extract_api_payloads(
                    response_payloads
                )
                if captured_rows:
                    file_rows = captured_rows
                share_id = share_id or captured_meta.get("shareid", "")
                uk = uk or captured_meta.get("uk", "")
                sign = sign or captured_meta.get("sign", "")
                timestamp = timestamp or captured_meta.get("timestamp", "")
                randsk = randsk or captured_meta.get("randsk", "")

                if not file_rows:
                    api_rows, api_meta = await self._browser_shorturlinfo(
                        page, surl, js_token, dp_logid
                    )
                    file_rows = api_rows
                    share_id = share_id or api_meta.get("shareid", "")
                    uk = uk or api_meta.get("uk", "")
                    sign = sign or api_meta.get("sign", "")
                    timestamp = timestamp or api_meta.get("timestamp", "")
                    randsk = randsk or api_meta.get("randsk", "")

            log.info(
                "TeraBox API metadata: files=%s share_id=%s uk=%s",
                len(file_rows),
                bool(share_id),
                bool(uk),
            )

            direct = next(
                (row for row in file_rows if row.get("direct_url")),
                None,
            )

            if not direct and js_token:
                for row in file_rows:
                    if row.get("is_dir") or not row.get("fs_id"):
                        continue

                    dlink = await self._browser_download(
                        page=page,
                        js_token=js_token,
                        share_id=share_id,
                        uk=uk,
                        sign=sign or row.get("sign", ""),
                        timestamp=timestamp or row.get("timestamp", ""),
                        fs_id=row["fs_id"],
                    )
                    if dlink:
                        row["direct_url"] = dlink
                        direct = row
                        break

            # Fallback: the working reference implementation can stream a
            # video through TeraBox's HLS endpoint even when /share/download
            # does not return a normal dlink.
            stream = None
            if not direct and share_id and uk:
                for row in file_rows:
                    if row.get("is_dir") or not row.get("fs_id"):
                        continue

                    file_name = str(row.get("file_name") or "").lower()
                    if not any(file_name.endswith(ext) for ext in STREAM_VIDEO_EXTENSIONS):
                        continue

                    row_sign = row.get("sign") or sign
                    row_timestamp = row.get("timestamp") or timestamp
                    stream_url = _build_stream_url(
                        share_id=share_id,
                        uk=uk,
                        fs_id=row["fs_id"],
                        sign=row_sign,
                        timestamp=row_timestamp,
                    )
                    if stream_url:
                        row["stream_url"] = stream_url
                        stream = row
                        break

            if not direct and not stream:
                debug = await page.evaluate(
                    """
                    () => ({
                      title: document.title,
                      bodyText: (document.body?.innerText || '').slice(0, 1200),
                      resourceUrls: performance.getEntriesByType('resource')
                        .map(x => x.name)
                        .filter(x => /terabox|download|shorturl|share\//i.test(x))
                        .slice(-80)
                    })
                    """
                )
                def _safe_url(value: str) -> str:
                    try:
                        parsed = urlparse(value)
                        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    except Exception:
                        return value.split("?", 1)[0]

                log_line = {
                    "responses": [
                        {
                            **item,
                            "url": _safe_url(str(item.get("url") or "")),
                        }
                        for item in responses[-20:]
                    ],
                    "captured_payloads": [
                        {
                            "url": _safe_url(str(item.get("url") or "")),
                            "status": item.get("status"),
                            "content_type": item.get("content_type"),
                            "body": item.get("body", "")[:2000],
                        }
                        for item in response_payloads[-10:]
                    ],
                    "page": debug,
                }
                print(
                    "resolve diagnostics=" + json.dumps(log_line, ensure_ascii=False)[:12000],
                    flush=True,
                )
                raise RuntimeError(
                    "Chromium loaded the TeraBox page but no direct download or stream URL was exposed."
                )

            selected = direct or stream
            download_mode = "direct" if direct else "stream"

            print(
                f"resolve success file={selected.get('file_name') or '<unknown>'} "
                f"size={selected.get('size', 0)} mode={download_mode}",
                flush=True,
            )

            return {
                "success": True,
                "file_name": selected.get("file_name") or "terabox-file",
                "size": int(selected.get("size") or 0),
                "fs_id": selected.get("fs_id") or "",
                "direct_url": selected.get("direct_url") or "",
                "stream_url": selected.get("stream_url") or "",
                "download_mode": download_mode,
                "thumbnail": selected.get("thumbnail") or "",
                "sign": selected.get("sign") or sign,
                "timestamp": selected.get("timestamp") or timestamp,
                "share_id": share_id,
                "uk": uk,
                "duration": int(selected.get("duration") or 0),
                "cookies": "; ".join(
                    f"{cookie['name']}={cookie['value']}"
                    for cookie in await page.context.cookies()
                ) or browser_data.get("cookies") or "",
                "randsk": randsk,
                "referer_url": page.url,
                "files": file_rows[:50] or [selected],
                "surl": surl,
            }
        finally:
            try:
                page.remove_listener("response", capture)
            except Exception:
                pass

    @staticmethod
    def _errno(data: Any) -> int:
        if not isinstance(data, dict):
            return 0
        try:
            return int(data.get("errno") or data.get("code") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _extract_api_payloads(
        payloads: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        for item in reversed(payloads):
            try:
                data = json.loads(item.get("body", ""))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            try:
                if int(data.get("errno") or data.get("code") or 0):
                    continue
            except (TypeError, ValueError):
                pass

            rows = _rows(data)
            meta = data.get("data") if isinstance(data.get("data"), dict) else data
            meta_out = {
                "shareid": str(
                    meta.get("shareid")
                    or meta.get("share_id")
                    or meta.get("SHARE_ID")
                    or ""
                ),
                "uk": str(meta.get("uk") or meta.get("SHARE_UK") or ""),
                "sign": str(meta.get("sign") or meta.get("SIGN") or ""),
                "timestamp": str(
                    meta.get("timestamp") or meta.get("TIMESTAMP") or ""
                ),
                "randsk": str(meta.get("randsk") or meta.get("RANDSK") or ""),
            }
            if rows:
                return rows, meta_out
        return [], {}

    async def _browser_share_list(
        self,
        page: Page,
        surl: str,
        js_token: str,
        dp_logid: str,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        candidates = [surl]
        if surl.startswith("1"):
            candidates.append(surl[1:])
        else:
            candidates.insert(0, "1" + surl)

        for shorturl in candidates:
            params = {
                "app_id": APP_ID,
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "root": "1",
                "page": "1",
                "num": "100",
                "shorturl": shorturl,
                "jsToken": js_token,
            }
            if dp_logid:
                params["dp-logid"] = dp_logid

            result = await page.evaluate(
                """
                async ({params}) => {
                  const u = new URL('/share/list', location.origin);
                  for (const [key, value] of Object.entries(params))
                    u.searchParams.set(key, value);
                  const response = await fetch(u.toString(), {
                    credentials: 'include',
                    headers: {
                      'Accept': 'application/json, text/plain, */*',
                      'X-Requested-With': 'XMLHttpRequest'
                    }
                  });
                  const text = await response.text();
                  let data = null;
                  try { data = JSON.parse(text); } catch (_) {}
                  return {status: response.status, data, text: text.slice(0, 3000)};
                }
                """,
                {"params": params},
            )

            print(
                f"share/list shorturl={shorturl} status={result.get('status')} "
                f"data={'yes' if isinstance(result.get('data'), dict) else 'no'}",
                flush=True,
            )

            data = result.get("data")
            if not isinstance(data, dict) or self._errno(data):
                continue

            rows = _rows(data)
            if not rows:
                continue

            meta = data.get("data") if isinstance(data.get("data"), dict) else data
            return rows, {
                "shareid": str(
                    meta.get("shareid")
                    or meta.get("share_id")
                    or meta.get("SHARE_ID")
                    or ""
                ),
                "uk": str(meta.get("uk") or meta.get("SHARE_UK") or ""),
                "sign": str(meta.get("sign") or meta.get("SIGN") or ""),
                "timestamp": str(
                    meta.get("timestamp") or meta.get("TIMESTAMP") or ""
                ),
                "randsk": str(meta.get("randsk") or meta.get("RANDSK") or ""),
            }

        return [], {}

    async def _browser_shorturlinfo(
        self,
        page: Page,
        surl: str,
        js_token: str,
        dp_logid: str,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        for shorturl in ("1" + surl, surl):
            params = {
                "app_id": APP_ID,
                "shorturl": shorturl,
                "root": "1",
                "jsToken": js_token,
            }
            if dp_logid:
                params["dp-logid"] = dp_logid

            result = await page.evaluate(
                """
                async ({params}) => {
                  const u = new URL('/api/shorturlinfo', location.origin);
                  for (const [key, value] of Object.entries(params))
                    u.searchParams.set(key, value);

                  const response = await fetch(u.toString(), {
                    credentials: 'include',
                    headers: {
                      'Accept': 'application/json, text/plain, */*',
                      'X-Requested-With': 'XMLHttpRequest'
                    }
                  });
                  const text = await response.text();
                  let data = null;
                  try { data = JSON.parse(text); } catch (_) {}
                  return {status: response.status, data, text: text.slice(0, 1000)};
                }
                """,
                {"params": params},
            )

            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict):
                continue

            rows = _rows(data)
            if not rows:
                continue

            meta = data.get("data") if isinstance(data.get("data"), dict) else data
            return rows, {
                "shareid": str(
                    meta.get("shareid")
                    or meta.get("share_id")
                    or meta.get("SHARE_ID")
                    or ""
                ),
                "uk": str(meta.get("uk") or meta.get("SHARE_UK") or ""),
                "sign": str(meta.get("sign") or meta.get("SIGN") or ""),
                "timestamp": str(
                    meta.get("timestamp") or meta.get("TIMESTAMP") or ""
                ),
                "randsk": str(meta.get("randsk") or meta.get("RANDSK") or ""),
            }

        return [], {}

    async def _browser_download(
        self,
        *,
        page: Page,
        js_token: str,
        share_id: str,
        uk: str,
        sign: str,
        timestamp: str,
        fs_id: str,
    ) -> str:
        if not all((js_token, share_id, uk, fs_id)):
            return ""

        fid_json = json.dumps([int(fs_id)])
        base_params = {
            "app_id": APP_ID,
            "web": "1",
            "channel": "dubox",
            "clienttype": "0",
            "jsToken": js_token,
            "shareid": share_id,
            "uk": uk,
            "primaryid": share_id,
            "product": "share",
            "nozip": "0",
            "fid_list": fid_json,
        }

        # This mirrors the working TeraBox implementation: use the browser's
        # live session and the GET form of /share/download.
        result = await page.evaluate(
            """
            async ({params}) => {
              const u = new URL('/share/download', location.origin);
              for (const [key, value] of Object.entries(params))
                u.searchParams.set(key, value);

              const response = await fetch(u.toString(), {
                method: 'GET',
                credentials: 'include',
                headers: {
                  'Accept': 'application/json, text/plain, */*',
                  'X-Requested-With': 'XMLHttpRequest',
                  'Referer': 'https://www.terabox.app/'
                }
              });

              const text = await response.text();
              let data = null;
              try { data = JSON.parse(text); } catch (_) {}

              return {
                status: response.status,
                data,
                text: text.slice(0, 4000)
              };
            }
            """,
            {"params": base_params},
        )

        print(
            f"share/download status={result.get('status')} "
            f"data={'yes' if isinstance(result.get('data'), dict) else 'no'}",
            flush=True,
        )

        dlink = self._extract_dlink(
            result.get("data") if isinstance(result, dict) else None
        )
        if dlink:
            return dlink

        # Some TeraBox variants reject the first request unless the web
        # client parameters seen in the original share/list request are also
        # present. Search the page's performance entries for those parameters
        # and retry within the same browser session.
        extra = await page.evaluate(
            """
            () => {
              const urls = performance.getEntriesByType('resource')
                .map(x => x.name)
                .filter(x => /\\/share\\/list\\?/i.test(x));
              return urls.length ? urls[urls.length - 1] : '';
            }
            """
        )

        if extra:
            result = await page.evaluate(
                """
                async ({sourceUrl, shareId, uk, fid}) => {
                  const source = new URL(sourceUrl);
                  const u = new URL('/share/download', location.origin);

                  for (const [key, value] of source.searchParams.entries()) {
                    if (key !== 'shorturl' && key !== 'page' && key !== 'num') {
                      u.searchParams.set(key, value);
                    }
                  }

                  u.searchParams.set('app_id', '250528');
                  u.searchParams.set('shareid', shareId);
                  u.searchParams.set('uk', uk);
                  u.searchParams.set('primaryid', shareId);
                  u.searchParams.set('product', 'share');
                  u.searchParams.set('nozip', '0');
                  u.searchParams.set('fid_list', JSON.stringify([Number(fid)]));

                  const response = await fetch(u.toString(), {
                    credentials: 'include',
                    headers: {
                      'Accept': 'application/json, text/plain, */*',
                      'X-Requested-With': 'XMLHttpRequest',
                      'Referer': 'https://www.terabox.app/'
                    }
                  });

                  const text = await response.text();
                  let data = null;
                  try { data = JSON.parse(text); } catch (_) {}

                  return {
                    status: response.status,
                    data,
                    text: text.slice(0, 4000)
                  };
                }
                """,
                {
                    "sourceUrl": extra,
                    "shareId": share_id,
                    "uk": uk,
                    "fid": fs_id,
                },
            )

            print(
                f"share/download replay status={result.get('status')} "
                f"data={'yes' if isinstance(result.get('data'), dict) else 'no'}",
                flush=True,
            )

            dlink = self._extract_dlink(
                result.get("data") if isinstance(result, dict) else None
            )
            if dlink:
                return dlink

        diagnostic = {
            "status": result.get("status") if isinstance(result, dict) else None,
            "text": result.get("text", "")[:1200] if isinstance(result, dict) else "",
            "has_sign": bool(sign),
            "has_timestamp": bool(timestamp),
        }
        print(
            "share/download diagnostics="
            + json.dumps(diagnostic, ensure_ascii=False)[:2500],
            flush=True,
        )
        return ""

    @staticmethod
    def _extract_dlink(data: Any) -> str:
        if not isinstance(data, dict):
            return ""

        direct = str(data.get("dlink") or "").strip()
        if direct:
            return direct

        nested = data.get("data")
        if isinstance(nested, dict):
            direct = str(nested.get("dlink") or "").strip()
            if direct:
                return direct

        rows = _rows(data)
        for row in rows:
            if row.get("direct_url"):
                return row["direct_url"]

        return ""
