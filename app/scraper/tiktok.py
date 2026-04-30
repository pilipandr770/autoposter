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

    new_videos = []
    entries = info.get("entries", [])

    for entry in entries:
        vid_id = str(entry.get("id", ""))
        if not vid_id:
            continue

        # Если дошли до уже известного видео — стоп
        if last_known_id and vid_id == last_known_id:
            break

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
        "--playlist-end", "10",        # только последние 10
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
        "--no-watermark",           # убирает watermark TikTok если поддерживается
        "--no-warnings",
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


def cleanup_old_media(keep_last: int = 50):
    """Удаляет старые медиафайлы, оставляя только последние N."""
    try:
        files = sorted(
            [os.path.join(settings.MEDIA_DIR, f) for f in os.listdir(settings.MEDIA_DIR)],
            key=os.path.getmtime
        )
        for f in files[:-keep_last]:
            os.remove(f)
            logger.debug(f"Deleted old media: {f}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
