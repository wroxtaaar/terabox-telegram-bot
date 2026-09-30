from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
import logging


log = logging.getLogger("terabox-vps-worker")


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
    http_session: aiohttp.ClientSession | None = None,
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
            f"{effective_cookie}; TSID={randsk}"
            if effective_cookie
            else f"TSID={randsk}"
        )
    if effective_cookie:
        headers["Cookie"] = effective_cookie

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=20,
        sock_read=120,
    )
    discovered: dict[int, tuple[str, int]] = {}

    owned_session = http_session is None
    session = http_session or aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(
            limit=16,
            limit_per_host=16,
            ttl_dns_cache=300,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
        ),
    )

    try:
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
            # The time probes are small requests. Fetch a few at once to avoid
            # turning long videos into hundreds of serial 25-second API calls.
            try:
                probe_concurrency = max(
                    1,
                    min(8, int(os.getenv("HLS_PROBE_CONCURRENCY", "4"))),
                )
            except (TypeError, ValueError):
                probe_concurrency = 4

            async def probe_time(t: int) -> tuple[int, str]:
                time_query = {
                    "app_id": "250528",
                    "web": "1",
                    "channel": "dubox",
                    "clienttype": "0",
                    "shareid": effective_share_id,
                    "uk": effective_uk,
                    "fid": effective_fid,
                    "sign": effective_sign,
                    "timestamp": effective_timestamp,
                    "type": "M3U8_AUTO_480",
                    "time": str(t),
                    "esl": "1",
                    "isplayer": "1",
                    "ehps": "1",
                }
                time_url = "https://www.terabox.app/share/streaming?" + urlencode(time_query)
                try:
                    return await _fetch_text(session, time_url, headers)
                except Exception:
                    return 0, ""

            # Probe in bounded batches and keep the existing de-duplication
            # semantics. Results are merged only after the batch is complete.
            times = list(range(step, max_scan_time + 1, step))
            for batch_start in range(0, len(times), probe_concurrency):
                batch = times[batch_start : batch_start + probe_concurrency]
                results = await asyncio.gather(
                    *(probe_time(t) for t in batch),
                    return_exceptions=True,
                )
                before = len(discovered)
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    time_status, time_playlist = result
                    if time_status < 200 or time_status >= 300 or not time_playlist:
                        continue
                    discovered.update(_parse_segments(time_playlist, m3u8_url))

                # For streams where duration is missing, stop after a few
                # consecutive empty batches once at least one segment is known.
                if not duration and discovered and len(discovered) == before:
                    # A batch is 4*25s by default; two empty batches means no
                    # further timeline data was found.
                    if batch_start >= probe_concurrency * step * 2:
                        break

        indices = sorted(discovered)
        if not indices:
            raise RuntimeError("M3U8 playlist contained no video segments.")

        temp_ts = output_path.with_suffix(output_path.suffix + ".temp.ts")
        temp_output = output_path.with_name(
            f"{output_path.stem}.remux{output_path.suffix or '.mp4'}"
        )
        segment_dir = output_path.parent / f"{output_path.stem}.segments"
        total_bytes = 0

        try:
            segment_dir.mkdir(parents=True, exist_ok=True)
            total_chunks = len(indices)
            try:
                concurrency = max(
                    1,
                    min(8, int(os.getenv("HLS_SEGMENT_CONCURRENCY", "6"))),
                )
            except (TypeError, ValueError):
                concurrency = 6

            completed = 0
            progress_lock = asyncio.Lock()
            semaphore = asyncio.Semaphore(concurrency)

            async def fetch_segment(position: int, index: int) -> tuple[int, Path, int]:
                nonlocal completed
                segment_url, declared_size = discovered[index]
                part_path = segment_dir / f"{position:05d}.part.ts"

                for attempt in range(4):
                    async with semaphore:
                        try:
                            async with session.get(
                                segment_url,
                                headers=headers,
                                allow_redirects=True,
                            ) as response:
                                if response.status < 200 or response.status >= 300:
                                    raise RuntimeError(
                                        f"HTTP {response.status}"
                                    )

                                segment_bytes = 0
                                with part_path.open("wb") as part:
                                    async for chunk in response.content.iter_chunked(4 * 1024 * 1024):
                                        segment_bytes += len(chunk)
                                        if segment_bytes > 256 * 1024 * 1024:
                                            raise RuntimeError("HLS segment exceeded safety limit.")
                                        part.write(chunk)

                            if declared_size and segment_bytes < declared_size:
                                raise RuntimeError(
                                    f"short HLS segment: expected at least {declared_size}, got {segment_bytes}"
                                )
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            part_path.unlink(missing_ok=True)
                            if attempt >= 3:
                                raise
                            await asyncio.sleep(0.35 * (2 ** attempt))
                            log.debug(
                                "HLS segment retry position=%s attempt=%s: %s",
                                position,
                                attempt + 1,
                                exc,
                            )

                async with progress_lock:
                    completed += 1
                    percent = round(completed * 100 / total_chunks)
                    if progress:
                        result = progress(percent, completed, total_chunks)
                        if asyncio.iscoroutine(result):
                            await result

                return position, part_path, segment_bytes

            #             results = await asyncio.gather(
                *(
                    fetch_segment(position, index)
                    for position, index in enumerate(indices, 1)
                )
            )

            results.sort(key=lambda item: item[0])
            with temp_ts.open("wb") as output:
                for _, part_path, segment_bytes in results:
                    with part_path.open("rb") as part:
                        while True:
                            chunk = part.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            total_bytes += len(chunk)

            log.info(
                "HLS segment download complete chunks=%s bytes=%s concurrency=%s",
                total_chunks,
                total_bytes,
                concurrency,
            )

            ffmpeg = (
                os.getenv("FFMPEG_PATH", "").strip()
                or "ffmpeg"
            )
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
            import shutil
            shutil.rmtree(segment_dir, ignore_errors=True)

    finally:
        if owned_session:
            await session.close()

    size = output_path.stat().st_size if output_path.exists() else 0
    if size <= 0:
        raise RuntimeError("M3U8 download produced an empty output file.")

    return size, len(indices)
