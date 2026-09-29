from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp


ProgressCallback = Callable[[int, int, int], Awaitable[None] | None]


def _chunk_url_with_full_range(segment_url: str) -> tuple[int, str, int]:
    parsed = urlparse(segment_url)
    match = re.search(r"_(\d+)_ts\b", parsed.path, re.IGNORECASE)
    chunk_index = int(match.group(1)) if match else 0

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    try:
        ts_size = int(params.get("ts_size") or 0)
    except (TypeError, ValueError):
        ts_size = 0

    if ts_size > 0:
        params["range"] = f"0-{ts_size - 1}"
        params["len"] = str(ts_size)
        parsed = parsed._replace(query=urlencode(params))

    return chunk_index, urlunparse(parsed), ts_size


def _parse_segments(playlist_text: str, base_url: str) -> dict[int, tuple[str, int]]:
    discovered: dict[int, tuple[str, int]] = {}

    for raw_line in playlist_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        segment_url = urljoin(base_url, line)
        try:
            index, normalized_url, size = _chunk_url_with_full_range(segment_url)
        except Exception:
            index = 0
            normalized_url = segment_url
            size = 0

        if index <= 0:
            index = max(discovered.keys(), default=0) + 1

        discovered.setdefault(index, (normalized_url, size))

    return discovered


async def _fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
) -> tuple[int, str]:
    async with session.get(
        url,
        headers=headers,
        allow_redirects=True,
    ) as response:
        text = await response.text()
        return response.status, text


async def download_m3u8_stream(
    m3u8_url: str,
    output_path: Path,
    *,
    referer_url: str,
    cookie_header: str = "",
    duration: int = 0,
    share_id: str = "",
    uk: str = "",
    sign: str = "",
    timestamp: str = "",
    fs_id: str = "",
    randsk: str = "",
    progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    """
    Port of the working TeraBox M3U8 downloader strategy.

    TeraBox often exposes only a short initial HLS window. We collect the
    chunk URLs from the initial playlist and then probe the timeline using
    ?time=... to discover additional chunks. Each chunk is requested with its
    full ts_size range before FFmpeg remuxes the concatenated transport stream
    into a normal MP4.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": referer_url or "https://www.terabox.app/",
        "Accept": "*/*",
    }
    effective_cookie = cookie_header.strip()
    if randsk and "TSID=" not in effective_cookie:
        effective_cookie = (
            f"{effective_cookie}; TSID={randsk}" if effective_cookie else f"TSID={randsk}"
        )
    if effective_cookie:
        headers["Cookie"] = effective_cookie

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=20,
        sock_read=120,
    )
    discovered: dict[int, tuple[str, int]] = {}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        status, playlist = await _fetch_text(session, m3u8_url, headers)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Failed to fetch M3U8 playlist: HTTP {status}")

        discovered.update(_parse_segments(playlist, m3u8_url))

        parsed = urlparse(m3u8_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        effective_share_id = share_id or query.get("shareid", "")
        effective_uk = uk or query.get("uk", "")
        effective_fid = fs_id or query.get("fid", "")
        effective_sign = sign or query.get("sign", "")
        effective_timestamp = timestamp or query.get("timestamp", "")

        if effective_share_id and effective_uk and effective_fid and effective_sign and effective_timestamp:
            try:
                max_scan_time = max(30, int(duration) + 30) if duration else 7200
            except (TypeError, ValueError):
                max_scan_time = 7200

            step = 25
            empty_streak = 0

            # Preserve the complete signed query from the browser-exposed
            # stream URL while changing only the timeline position. In
            # particular, this keeps jsToken and any other browser-issued
            # parameters that are required for later HLS windows.
            base_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            base_query.setdefault("app_id", "250528")
            base_query.setdefault("web", "1")
            base_query.setdefault("channel", "dubox")
            base_query.setdefault("clienttype", "0")
            base_query.setdefault("shareid", effective_share_id)
            base_query.setdefault("uk", effective_uk)
            base_query.setdefault("fid", effective_fid)
            base_query.setdefault("sign", effective_sign)
            base_query.setdefault("timestamp", effective_timestamp)
            base_query.setdefault("type", "M3U8_AUTO_480")
            base_query.setdefault("esl", "1")
            base_query.setdefault("isplayer", "1")
            base_query.setdefault("ehps", "1")

            for t in range(step, max_scan_time + 1, step):
                time_query = dict(base_query)
                time_query["time"] = str(t)
                time_url = "https://www.terabox.app/share/streaming?" + urlencode(time_query)

                try:
                    time_status, time_playlist = await _fetch_text(
                        session,
                        time_url,
                        headers,
                    )
                    if time_status < 200 or time_status >= 300:
                        continue

                    before = len(discovered)
                    discovered.update(_parse_segments(time_playlist, time_url))

                    if len(discovered) > before:
                        empty_streak = 0
                    else:
                        empty_streak += 1
                        if not duration and empty_streak >= 4 and discovered:
                            break
                except Exception:
                    continue

        indices = sorted(discovered)
        if not indices:
            raise RuntimeError("M3U8 playlist contained no video segments.")

        temp_ts = output_path.with_suffix(output_path.suffix + ".temp.ts")
        # Keep a real container extension so FFmpeg can infer the output
        # format. A name ending only in ".tmp" makes FFmpeg report
        # "Unable to find a suitable output format".
        temp_output = output_path.with_name(
            f"{output_path.stem}.remux{output_path.suffix or '.mp4'}"
        )
        total_bytes = 0

        log = __import__("logging").getLogger("terabox-vps-worker")
        log.info(
            "HLS playlist discovered %s unique segment(s) for %s",
            len(indices),
            output_path.name,
        )

        try:
            with temp_ts.open("wb") as output:
                total_chunks = len(indices)

                for position, index in enumerate(indices, 1):
                    segment_url, _ = discovered[index]

                    async with session.get(
                        segment_url,
                        headers=headers,
                        allow_redirects=True,
                    ) as response:
                        if response.status < 200 or response.status >= 300:
                            raise RuntimeError(
                                f"Failed to fetch segment {position}/{total_chunks}: "
                                f"HTTP {response.status}"
                            )

                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            output.write(chunk)
                            total_bytes += len(chunk)

                    percent = round(position * 100 / total_chunks)
                    if progress:
                        result = progress(percent, position, total_chunks)
                        if asyncio.iscoroutine(result):
                            await result

            ffmpeg = (
                os.getenv("FFMPEG_PATH", "").strip()
                or "ffmpeg"
            )
            temp_output.unlink(missing_ok=True)
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(temp_ts),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(temp_output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                error_text = (stderr or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(
                    "FFmpeg remux failed"
                    + (f": {error_text[-1200:]}" if error_text else ".")
                )

            if not temp_output.exists() or temp_output.stat().st_size <= 0:
                raise RuntimeError("FFmpeg reported success but produced no output file.")

            temp_output.replace(output_path)

        finally:
            temp_ts.unlink(missing_ok=True)
            temp_output.unlink(missing_ok=True)

    size = output_path.stat().st_size if output_path.exists() else 0
    if size <= 0:
        raise RuntimeError("M3U8 download produced an empty output file.")

    return size, len(indices)
