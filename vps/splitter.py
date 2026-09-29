from __future__ import annotations

import asyncio
import math
import os
import subprocess
from pathlib import Path


MAX_TELEGRAM_FILE_SIZE = 48 * 1024 * 1024


async def get_video_duration(video_path: Path) -> float:
    ffprobe = os.getenv("FFPROBE_PATH", "").strip() or "ffprobe"
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return 0.0
    try:
        return float(stdout.decode("utf-8", "replace").strip())
    except ValueError:
        return 0.0


async def split_video(
    video_path: Path,
    output_dir: Path,
    max_size_bytes: int = MAX_TELEGRAM_FILE_SIZE,
) -> list[Path]:
    if video_path.stat().st_size <= max_size_bytes:
        return [video_path]

    duration = await get_video_duration(video_path)
    ext = video_path.suffix or ".mp4"
    base_name = video_path.stem
    parts_dir = output_dir / f"parts_{video_path.stem}"
    parts_dir.mkdir(parents=True, exist_ok=True)

    estimated_parts = max(2, math.ceil(video_path.stat().st_size / (max_size_bytes * 0.90)))

    if duration > 0 and estimated_parts > 1:
        segment_time = max(2, math.floor(duration / estimated_parts))
        pattern = parts_dir / f"seg_%03d{ext}"

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-map",
            "0",
            "-segment_time",
            str(segment_time),
            "-f",
            "segment",
            "-reset_timestamps",
            "1",
            str(pattern),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        generated = sorted(parts_dir.glob(f"seg_*{ext}"))
        if process.returncode == 0 and len(generated) > 1:
            if all(p.stat().st_size <= max_size_bytes for p in generated):
                total = len(generated)
                renamed: list[Path] = []
                for index, part in enumerate(generated, 1):
                    new_path = parts_dir / f"{base_name}.Part_{index}_of_{total}{ext}"
                    part.rename(new_path)
                    renamed.append(new_path)
                return renamed

    return split_binary_file(video_path, parts_dir, max_size_bytes)


def split_binary_file(
    file_path: Path,
    output_dir: Path,
    max_size_bytes: int = MAX_TELEGRAM_FILE_SIZE,
) -> list[Path]:
    size = file_path.stat().st_size
    if size <= max_size_bytes:
        return [file_path]

    parts_dir = output_dir / f"parts_{file_path.stem}"
    parts_dir.mkdir(parents=True, exist_ok=True)

    chunk_size = max(
        1024,
        max_size_bytes - 64 * 1024,
    )
    parts: list[Path] = []

    with file_path.open("rb") as source:
        index = 1
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            part_path = parts_dir / f"{file_path.name}.part{index:03d}"
            part_path.write_bytes(chunk)
            parts.append(part_path)
            index += 1

    return parts
