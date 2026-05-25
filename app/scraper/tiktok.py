import os
import json
import logging
import asyncio
import subprocess
from dataclasses import dataclass
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class TikTokVideo:
    id: str
    title: str
    file_path: str
    thumbnail: Optional[str] = None


def _id_to_int(video_id: str) -> int | None:
    try:
        return int(video_id)
    except Exception:
        return None


async def get_new_videos(username: str, last_known_id: str = None) -> list[TikTokVideo]:
    """
    Скачивает новые видео с TikTok профиля.
    Возвращает список новых видео (только те, что новее last_known_id).
    """
    if not username:
        logger.error("TikTok username not configured")
        return []

    os.makedirs(settings.MEDIA_DIR, exist_ok=True)

    url = f"https://www.tiktok.com/@{username}"

    # Сначала получаем список видео без скачивания
    try:
        info = await _get_playlist_info(url)
    except Exception as e:
        logger.error(f"TikTok: failed to get playlist: {e}")
        return []

    if not info:
        return []

    entries = info.get("entries", [])
    if not entries:
        return []

    playlist_debug = []
    for idx, e in enumerate(entries[:30], start=1):
        vid = str(e.get("id", ""))
        upload_date = e.get("upload_date") or "-"
        ts = e.get("timestamp") or "-"
        playlist_debug.append(f"{idx}:{vid}@{upload_date}/{ts}")
    logger.info("TikTok: playlist window (up to 30): " + ", ".join(playlist_debug))

    head_ids = [str(e.get("id", "")) for e in entries[:5]]
    logger.info(f"TikTok: top ids={head_ids}, last_known_id={last_known_id or '-'}")

    candidates = entries
    if last_known_id:
        last_idx = next(
            (i for i, e in enumerate(entries) if str(e.get("id", "")) == last_known_id),
            -1,
        )

        if last_idx > 0:
            candidates = entries[:last_idx]
        elif last_idx == 0:
            # last_known video can be pinned at the top; compare IDs instead of stopping immediately
            logger.warning(
                "TikTok: last_known_id is first in playlist, using ID comparison (possible pinned video)"
            )
            last_num = _id_to_int(last_known_id)
            if last_num is not None:
                candidates = [
                    e for e in entries
                    if (_id_to_int(str(e.get("id", ""))) or 0) > last_num
                ]
            else:
                candidates = []
        else:
            # last_known not present in current window; keep all and dedupe in DB layer
            candidates = entries

    new_videos = []

    for entry in candidates:
        vid_id = str(entry.get("id", ""))
        if not vid_id:
            continue

        title = entry.get("title") or entry.get("description") or "TikTok video"
        title = title[:200]

        # Скачиваем видео без watermark
        file_path = await _download_video(url, vid_id, username)
        if not file_path:
            logger.warning(f"TikTok: failed to download {vid_id}")
            continue

        new_videos.append(TikTokVideo(
            id=vid_id,
            title=title,
            file_path=file_path,
        ))

    return new_videos


async def _get_playlist_info(url: str) -> dict:
    """Получает метаданные без скачивания."""
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--flat-playlist",
        "--playlist-end", "30",        # берем окно шире, чтобы не пропускать из-за pinned/серий постов
        "--no-warnings",
        url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())

    entries = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass

    return {"entries": entries}


async def _download_video(profile_url: str, video_id: str, username: str) -> Optional[str]:
    """Скачивает конкретное видео."""
    output_dir = settings.MEDIA_DIR
    output_tmpl = os.path.join(output_dir, f"{username}_{video_id}.%(ext)s")

    # Ищем уже скачанный файл
    for ext in ("mp4", "webm", "mov"):
        existing = os.path.join(output_dir, f"{username}_{video_id}.{ext}")
        if os.path.exists(existing):
            return existing

    video_url = f"https://www.tiktok.com/@{username}/video/{video_id}"

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-warnings",
        "--postprocessor-args", "ffmpeg:-threads 2",  # ограничиваем CPU при merge
        "-o", output_tmpl,
        video_url
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"yt-dlp error: {stderr.decode()[:500]}")
        return None

    # Находим скачанный файл
    for ext in ("mp4", "webm", "mov"):
        path = os.path.join(output_dir, f"{username}_{video_id}.{ext}")
        if os.path.exists(path):
            return path

    return None


def cleanup_old_media(keep_last: int = 3):
    """Удаляет старые медиафайлы, оставляя только последние N."""
    try:
        files = sorted(
            [os.path.join(settings.MEDIA_DIR, f) for f in os.listdir(settings.MEDIA_DIR)],
            key=os.path.getmtime
        )
        to_delete = files[:-keep_last] if keep_last > 0 else files
        for f in to_delete:
            os.remove(f)
            logger.debug(f"Deleted old media: {f}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
