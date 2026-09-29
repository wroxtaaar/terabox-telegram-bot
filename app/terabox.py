"""TeraBox public-share resolver ported from the working TeraFetch implementation."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

log = logging.getLogger(__name__)

APP_ID = "250528"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

TERABOX_HOSTS = {
    "terabox.com",
    "www.terabox.com",
    "terabox.app",
    "www.terabox.app",
    "teraboxapp.com",
    "www.teraboxapp.com",
    "1024tera.com",
    "www.1024tera.com",
    "teraboxshare.com",
    "www.teraboxshare.com",
    "teraboxlink.com",
    "www.teraboxlink.com",
    "nephobox.com",
    "4funbox.com",
    "mirrobox.com",
    "momerybox.com",
    "tibibox.com",
    "freeterabox.com",
    "dubox.com",
}

BRIDGE_ENDPOINTS = [
    "https://terabox-api.mn-bots.workers.dev/download?url={url}",
    "https://terabox-dl.qtcloud.workers.dev/api?url={url}",
    "https://yt-api-terabox.vercel.app/api?url={url}",
]


def is_terabox_url(value: str) -> bool:
    try:
        host = (urlparse(value.strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in TERABOX_HOSTS or host.endswith(".terabox.com")


def extract_surl(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw)
    query = parse_qs(parsed.query)

    if query.get("surl"):
        key = query["surl"][0].strip()
    elif query.get("shorturl"):
        key = query["shorturl"][0].strip()
    else:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
        if match:
            key = match.group(1)
        else:
            parts = [p for p in parsed.path.split("/") if p]
            key = parts[-1] if parts and len(parts[-1]) >= 8 else ""

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,}", key):
        raise ValueError("Could not extract a TeraBox share code.")
    return key


def _candidate_keys(surl: str) -> list[str]:
    values = [surl]
    if surl.startswith("1"):
        values.append(surl[1:])
    else:
        values.append("1" + surl)
    return list(dict.fromkeys(values))


def _headers(referer: str, *, json_request: bool = False, cookie: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept": (
            "application/json, text/plain, */*"
            if json_request
            else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if json_request:
        headers["X-Requested-With"] = "XMLHttpRequest"
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _cookie_header(active_cookie: str, jar: aiohttp.CookieJar) -> str:
    parts: list[str] = []
    if active_cookie:
        value = active_cookie.strip()
        if value.startswith("ndus="):
            parts.append(value)
        else:
            parts.append(f"ndus={value}")
    parts.append("lang=en")
    for cookie in jar:
        item = f"{cookie.key}={cookie.value}"
        if item not in parts:
            parts.append(item)
    return "; ".join(parts)


def _parse_int(value) -> int:
    try:
        return int(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _normalize_file(item: dict, source_url: str, share_id: str = "", uk: str = "") -> dict:
    thumbs = item.get("thumbs") if isinstance(item.get("thumbs"), dict) else {}
    name = str(
        item.get("server_filename")
        or item.get("filename")
        or item.get("name")
        or "unnamed_file"
    ).strip()
    size = _parse_int(item.get("size"))
    fs_id = str(item.get("fs_id") or "").strip()
    dlink = str(
        item.get("dlink")
        or item.get("download_url")
        or item.get("downloadUrl")
        or ""
    ).strip()
    return {
        "file_name": name,
        "size": size,
        "fs_id": fs_id,
        "path": str(item.get("path") or ""),
        "is_dir": str(item.get("isdir") or "0") in {"1", "true", "True"},
        "direct_url": dlink,
        "thumbnail": str(
            thumbs.get("url3")
            or thumbs.get("url2")
            or thumbs.get("url1")
            or item.get("thumb")
            or ""
        ),
        "share_id": share_id,
        "uk": uk,
        "source_url": source_url,
    }


async def _fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
) -> tuple[str, aiohttp.ClientResponse]:
    response = await session.get(
        url,
        headers=headers,
        allow_redirects=True,
        timeout=timeout,
    )
    body = await response.text(errors="replace")
    return body, response


def _extract_yundata(html: str) -> tuple[str, str, str, str, list[dict]]:
    share_id = uk = sign = timestamp = ""
    file_list: list[dict] = []

    match = re.search(r"yunData\s*=\s*({[\s\S]*?});", html)
    if not match:
        return share_id, uk, sign, timestamp, file_list

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return share_id, uk, sign, timestamp, file_list

    share_id = str(data.get("SHARE_ID") or data.get("share_id") or "")
    uk = str(data.get("SHARE_UK") or data.get("uk") or "")
    sign = str(data.get("SIGN") or data.get("sign") or "")
    timestamp = str(data.get("TIMESTAMP") or data.get("timestamp") or "")
    if isinstance(data.get("FILE_LIST"), list):
        file_list = [x for x in data["FILE_LIST"] if isinstance(x, dict)]
    return share_id, uk, sign, timestamp, file_list


def _extract_jstoken(html: str) -> str:
    patterns = (
        r"fn%28%22([0-9a-fA-F]+)%22%29",
        r"fn\(\"([0-9a-fA-F]+)\"\)",
        r"\"jsToken\"\s*:\s*\"([^\"]+)\"",
        r"jsToken\s*=\s*['\"]([^'\"]+)['\"]",
        r"jsToken\s*:\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


async def _resolve_direct(
    session: aiohttp.ClientSession,
    surl: str,
    full_url: str,
    active_cookie: str,
) -> tuple[dict, list[dict]]:
    candidates = _candidate_keys(surl)
    primary = candidates[0]
    target_url = f"https://www.terabox.app/sharing/link?surl={quote(primary)}"

    # Mirror the working Google Studio implementation: fetch the desktop
    # sharing page, preserve the returned session cookies, then use tokenized
    # share/list and share/download endpoints on terabox.app.
    html = ""
    try:
        body, response = await _fetch_text(
            session,
            target_url,
            headers=_headers("https://www.terabox.app/", cookie=_cookie_header(active_cookie, session.cookie_jar)),
            timeout=12,
        )
        html = body
        log.warning(
            "TeraBox share page status=%s final=%s content_length=%s",
            response.status,
            response.url,
            len(html),
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("TeraBox share page fetch failed: %r", exc)

    js_token = _extract_jstoken(html)
    share_id, uk, sign, timestamp, file_list = _extract_yundata(html)
    merged_cookie = _cookie_header(active_cookie, session.cookie_jar)

    if js_token:
        log.warning("TeraBox jsToken extracted successfully")
    else:
        log.warning("TeraBox jsToken not found in sharing/link HTML")

    if share_id or uk:
        log.warning("TeraBox yunData found: share_id=%s uk=%s", bool(share_id), bool(uk))

    if not file_list or not any(str(x.get("dlink") or "").strip() for x in file_list):
        for key in candidates:
            endpoint = "https://www.terabox.app/share/list"
            params = {
                "app_id": APP_ID,
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "root": "1",
                "page": "1",
                "num": "100",
                "shorturl": key,
            }
            if js_token:
                params["jsToken"] = js_token
            try:
                async with session.get(
                    endpoint,
                    params=params,
                    headers=_headers("https://www.terabox.app/", json_request=True, cookie=merged_cookie),
                    allow_redirects=True,
                ) as response:
                    body = await response.text(errors="replace")
                    if response.status >= 400:
                        log.warning("TeraBox /share/list HTTP %s", response.status)
                        continue
                    data = json.loads(body)
                    if (
                        isinstance(data, dict)
                        and int(data.get("errno") or -1) == 0
                        and isinstance(data.get("list"), list)
                        and data["list"]
                    ):
                        file_list = [x for x in data["list"] if isinstance(x, dict)]
                        share_id = str(data.get("share_id") or share_id)
                        uk = str(data.get("uk") or uk)
                        log.warning("TeraBox /share/list returned %d items", len(file_list))
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as exc:
                log.warning("TeraBox /share/list failed for key=%s: %r", key, exc)

    if not file_list:
        raise RuntimeError("No files found in the TeraBox share response.")

    rows = [
        _normalize_file(item, full_url, share_id=share_id, uk=uk)
        for item in file_list
    ]

    # Match the working implementation's download fallback. The dlink is
    # short-lived, so generate it immediately after metadata extraction.
    for row in rows:
        if row["is_dir"] or row["direct_url"]:
            continue
        if not (js_token and share_id and uk and row["fs_id"]):
            continue

        params = {
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
            "fid_list": json.dumps([int(row["fs_id"])]),
        }
        try:
            async with session.get(
                "https://www.terabox.app/share/download",
                params=params,
                headers=_headers("https://www.terabox.app/", cookie=merged_cookie),
                allow_redirects=True,
            ) as response:
                body = await response.text(errors="replace")
                if response.status >= 400:
                    log.warning("TeraBox /share/download HTTP %s", response.status)
                    continue
                data = json.loads(body)
                if isinstance(data, dict) and int(data.get("errno") or -1) == 0:
                    row["direct_url"] = str(data.get("dlink") or "").strip()
                    if not row["direct_url"] and isinstance(data.get("data"), dict):
                        row["direct_url"] = str(data["data"].get("dlink") or "").strip()
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError) as exc:
            log.warning("TeraBox /share/download failed: %r", exc)

    usable = [row for row in rows if not row["is_dir"] and row["direct_url"]]
    if not usable:
        raise RuntimeError(
            "TeraBox metadata was found, but no downloadable dlink was returned."
        )

    return usable[0], rows


async def _resolve_bridge(
    session: aiohttp.ClientSession,
    clean_url: str,
) -> tuple[dict, list[dict]]:
    for template in BRIDGE_ENDPOINTS:
        endpoint = template.format(url=quote(clean_url, safe=""))
        try:
            async with session.get(
                endpoint,
                headers=_headers("https://www.terabox.app/"),
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10, connect=5),
            ) as response:
                if response.status >= 400:
                    log.warning("Bridge HTTP %s: %s", response.status, urlparse(endpoint).netloc)
                    continue
                data = json.loads(await response.text(errors="replace"))
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            log.warning("Bridge failed %s: %r", urlparse(endpoint).netloc, exc)
            continue

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("list"), list):
            items = data["list"]
        elif isinstance(data, dict) and isinstance(data.get("download_links"), list):
            items = data["download_links"]
        elif isinstance(data, dict) and any(
            data.get(k) for k in ("download_url", "dlink", "downloadUrl", "direct_link", "url")
        ):
            items = [data]
        else:
            continue

        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            direct = str(
                item.get("download_url")
                or item.get("dlink")
                or item.get("downloadUrl")
                or item.get("direct_link")
                or item.get("url")
                or item.get("download")
                or ""
            ).strip()
            if not direct:
                continue
            rows.append(
                {
                    "file_name": str(
                        item.get("file_name")
                        or item.get("filename")
                        or item.get("server_filename")
                        or item.get("name")
                        or "terabox_download"
                    ).strip(),
                    "size": _parse_int(item.get("size") or item.get("file_size")),
                    "fs_id": str(item.get("fs_id") or item.get("fid") or ""),
                    "path": str(item.get("path") or ""),
                    "is_dir": False,
                    "direct_url": direct,
                    "thumbnail": str(item.get("thumbnail") or item.get("thumb") or ""),
                    "share_id": "",
                    "uk": "",
                    "source_url": clean_url,
                }
            )

        if rows:
            log.warning("Bridge %s resolved %d file(s)", urlparse(endpoint).netloc, len(rows))
            return rows[0], rows

    raise RuntimeError("All external TeraBox bridge resolvers failed.")


def _result(surl: str, row: dict, files: list[dict]) -> dict:
    return {
        "surl": surl,
        "file_name": row["file_name"],
        "size": row["size"],
        "fs_id": row["fs_id"],
        "direct_url": row["direct_url"],
        "files": files[:50],
        "thumbnail": row["thumbnail"],
    }


async def resolve_terabox_url(url: str) -> dict:
    if not is_terabox_url(url):
        raise ValueError("Unsupported TeraBox URL.")

    surl = extract_surl(url)
    clean_url = f"https://terabox.com/s/{_candidate_keys(surl)[-1]}"
    active_cookie = os.getenv("TERABOX_COOKIE", "").strip()

    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=TIMEOUT,
        cookie_jar=aiohttp.CookieJar(),
    ) as session:
        try:
            row, files = await _resolve_direct(
                session,
                surl,
                url,
                active_cookie,
            )
            return _result(surl, row, files)
        except Exception as direct_exc:
            log.warning("Working direct TeraBox resolver failed: %r", direct_exc)

        try:
            row, files = await _resolve_bridge(session, clean_url)
            return _result(surl, row, files)
        except Exception as bridge_exc:
            raise RuntimeError(
                f"TeraBox resolution failed. Direct: {direct_exc}; Bridge: {bridge_exc}"
            ) from bridge_exc
