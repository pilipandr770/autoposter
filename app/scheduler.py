import asyncio
import logging
import os
from app.db import (
    get_video, save_video, mark_posted, mark_error,
    get_setting, set_setting
)
from app.scraper.tiktok import get_new_videos, cleanup_old_media
from app.publishers import youtube
from config import settings

logger = logging.getLogger(__name__)

_running = False
_checking = False  # prevent concurrent check_and_post() calls
_retry_checking = False  # prevent concurrent retry checks


def _posted(row: dict | None, platform_col: str) -> bool:
    return bool(row and row.get(platform_col))


async def _retry_failed_videos():
    """Попытка переопубликации видео с ошибками (posted_fb=0, posted_ig=0, и т.д.)."""
    global _retry_checking
    if _retry_checking:
        return
    _retry_checking = True
    try:
        await _do_retry_failed_videos()
    finally:
        _retry_checking = False


async def _do_retry_failed_videos():
    """Переопубликация видео с ошибками для всех платформ."""
    logger.debug("_do_retry_failed_videos: Starting retry attempt...")
    try:
        import sqlite3
        
        logger.debug("_do_retry_failed_videos: Connecting to DB...")
        conn = sqlite3.connect(settings.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Получить видео, которые не были успешно опубликованы
        logger.debug("_do_retry_failed_videos: Querying failed videos...")
        cursor.execute("""
            SELECT * FROM videos 
            WHERE posted_yt = 0
            AND error IS NOT NULL
            AND error NOT LIKE 'Media file deleted%'
            ORDER BY created_at DESC
            LIMIT 5
        """)
        rows = cursor.fetchall()
        conn.close()
        
        logger.debug(f"_do_retry_failed_videos: Found {len(rows) if rows else 0} rows")
        
        if not rows:
            logger.debug("No failed videos to retry")
            return
        
        logger.info(f"Retrying {len(rows)} failed video(s)")

        
        for row in rows:
            video_id = row["tiktok_id"]   # must be tiktok_id — mark_error/mark_posted use WHERE tiktok_id=?
            file_path = row["file_path"]
            title = row["title"]
            
            # Пропустить, если файл удален — пометить как deleted чтобы больше не попадал в очередь
            if not os.path.exists(file_path):
                logger.warning(f"Media file missing for retry: {file_path} — marking as deleted")
                mark_error(video_id, "Media file deleted - skip retry")
                continue
            
            yt_enabled = get_setting("enable_youtube", "1") == "1"

            logger.info(f"Retrying: {title[:60]}")

            # YouTube
            if yt_enabled and not row["posted_yt"]:
                ok = await youtube.post_video(file_path, title, title)
                if ok:
                    mark_posted(video_id, "youtube")
                    logger.info(f"✅ YouTube retry success: {title[:60]}")
                await asyncio.sleep(settings.POST_DELAY_SECONDS)
            
            # Очистить ошибку если все успешно
            conn = sqlite3.connect(settings.DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
            updated = cursor.fetchone()
            conn.close()
            
            if updated:
                all_success = True
                if yt_enabled and not updated["posted_yt"]:
                    all_success = False
                
                if all_success:
                    mark_error(video_id, None)
                    logger.info(f"🎉 Video fully published: {title[:60]}")
                    # Delete media — no longer needed after all platforms done
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logger.info(f"🗑️ Deleted media after retry: {file_path}")
                        except Exception:
                            pass
                        base = file_path.rsplit(".", 1)[0]
                        for suffix in ("_yt.mp4",):
                            tmp = base + suffix
                            if os.path.exists(tmp):
                                try:
                                    os.remove(tmp)
                                except Exception:
                                    pass

            # Pause between retry videos — same breathing room as normal cycle
            if True:  # always pause (could gate on index but simpler unconditional)
                logger.info("⏸️ Pause 180s before next retry...")
                await asyncio.sleep(180)
    
    except Exception as e:
        logger.error(f"Retry failed videos error: {e}", exc_info=True)


async def check_and_post():
    """Главный цикл: проверяем TikTok → постим новые видео на все платформы."""
    global _checking
    logger.info("🔵 check_and_post: called, _checking=" + str(_checking))
    logger.info(f"_checking value: {_checking}")
    if _checking:
        logger.warning("Check already in progress, skipping duplicate call")
        return
    _checking = True
    logger.info("_checking set to True")
    try:
        logger.info("📋 Starting check_and_post cycle...")
        # Первый проход: переопубликация видео с ошибками
        logger.info("🔄 Phase 1: Retrying failed videos...")
        await _retry_failed_videos()
        # Второй проход: проверка новых видео с TikTok
        logger.info("🔍 Phase 2: Checking TikTok for new videos...")
        await _do_check_and_post()
        logger.info("✅ check_and_post cycle completed")
    except Exception as e:
        logger.error(f"check_and_post error: {e}", exc_info=True)
    finally:
        _checking = False
        logger.info("_checking reset to False")


async def _do_check_and_post():
    username = get_setting("tiktok_username") or settings.TIKTOK_USERNAME
    if not username:
        logger.warning("TikTok username not set, skipping check")
        return

    logger.info(f"🔍 Checking TikTok: @{username}")
    last_id = get_setting("last_tiktok_id")

    try:
        new_videos = await get_new_videos(username, last_known_id=last_id)
    except Exception as e:
        logger.error(f"TikTok scrape failed: {e}")
        return

    if not new_videos:
        logger.info("No new videos found")
        return

    logger.info(f"Found {len(new_videos)} new video(s)")

    # Process at most 3 videos per cycle — the rest are handled next cycle (30 min later).
    # This prevents 60-min continuous Chrome bursts when many videos accumulate.
    MAX_VIDEOS_PER_CYCLE = 3
    if len(new_videos) > MAX_VIDEOS_PER_CYCLE:
        logger.info(f"Limiting to {MAX_VIDEOS_PER_CYCLE} videos this cycle ({len(new_videos)} found)")
        new_videos = new_videos[:MAX_VIDEOS_PER_CYCLE]

    INTER_VIDEO_PAUSE = 180  # 3 min between videos — lets Chrome fully close, CPU cool down

    for i, video in enumerate(new_videos):
        existing = get_video(video.id)
        if existing and existing.get("posted_yt"):
            continue

        if not existing:
            save_video(video.id, video.title, video.file_path)
        logger.info(f"📹 Processing: {video.title[:60]}")

        errors = []
        posted_to = []

        # YouTube (единственная платформа для TikTok-потока)
        yt_enabled = get_setting("enable_youtube", "1") == "1"
        if yt_enabled and not _posted(existing, "posted_yt"):
            ok = await youtube.post_video(video.file_path, video.title, video.title)
            if ok:
                mark_posted(video.id, "youtube")
                posted_to.append("youtube")
            else:
                errors.append("YouTube")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        if errors:
            mark_error(video.id, f"Failed: {', '.join(errors)}")

        # Удаляем медиафайл только если все включенные платформы прошли.
        # Иначе файл нужен для повторной попытки недоопубликованных платформ.
        if posted_to and not errors and os.path.exists(video.file_path):
            try:
                os.remove(video.file_path)
                logger.info(f"🗑️ Deleted media after posting: {video.file_path}")
            except Exception as e:
                logger.warning(f"Could not delete media file: {e}")
            # Remove transcoded copies created by publishers
            base = video.file_path.rsplit(".", 1)[0]
            for suffix in ("_yt.mp4",):
                tmp = base + suffix
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

        # Pause between videos — lets Chrome fully close and CPU cool down
        if i < len(new_videos) - 1:
            logger.info(f"⏸️ Pause {INTER_VIDEO_PAUSE}s before next video...")
            await asyncio.sleep(INTER_VIDEO_PAUSE)

    # Сохраняем ID последнего видео
    if new_videos:
        set_setting("last_tiktok_id", new_videos[0].id)

    cleanup_old_media(keep_last=3)


async def scheduler_loop():
    global _running
    _running = True
    interval = settings.CHECK_INTERVAL_MINUTES * 60

    logger.info(f"⏰ Scheduler started. Interval: {settings.CHECK_INTERVAL_MINUTES} min")

    # Даём контейнеру время стабилизироваться перед первым запуском
    logger.info("⏳ Waiting 60s before first check (startup delay)...")
    await asyncio.sleep(60)

    while _running:
        try:
            logger.info("📌 scheduler_loop iteration: calling check_and_post()")
            await check_and_post()
            logger.info("📌 scheduler_loop iteration: check_and_post() completed")
        except Exception as e:
            logger.error(f"Scheduler error in main loop: {e}", exc_info=True)
        await asyncio.sleep(interval)


def stop_scheduler():
    global _running
    _running = False
