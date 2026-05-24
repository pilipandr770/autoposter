import asyncio
import logging
import os
from app.db import (
    get_video, save_video, mark_posted, mark_error,
    get_setting, set_setting
)
from app.scraper.tiktok import get_new_videos, cleanup_old_media
from app.publishers import instagram, youtube, telegram, facebook
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
            WHERE (posted_fb = 0 OR posted_ig = 0 OR posted_yt = 0 OR posted_tg = 0)
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
            
            ig_enabled = get_setting("enable_instagram", "0") == "1"
            fb_enabled = get_setting("enable_facebook", "1") == "1"
            yt_enabled = get_setting("enable_youtube", "1") == "1"
            tg_enabled = get_setting("enable_telegram", "0") == "1"
            ig_via_facebook = ig_enabled and fb_enabled
            
            logger.info(f"Retrying: {title[:60]}")
            
            # Instagram (только если не через Facebook)
            if ig_enabled and not ig_via_facebook and not row["posted_ig"]:
                ok = await instagram.post_reel(file_path, title)
                if ok:
                    mark_posted(video_id, "instagram")
                    logger.info(f"✅ Instagram retry success: {title[:60]}")
                await asyncio.sleep(settings.POST_DELAY_SECONDS)
            
            # YouTube
            if yt_enabled and not row["posted_yt"]:
                ok = await youtube.post_video(file_path, title, title)
                if ok:
                    mark_posted(video_id, "youtube")
                    logger.info(f"✅ YouTube retry success: {title[:60]}")
                await asyncio.sleep(settings.POST_DELAY_SECONDS)
            
            # Facebook
            if fb_enabled and not row["posted_fb"]:
                ok = await facebook.post_video(
                    file_path,
                    title,
                    crosspost_to_instagram=ig_via_facebook,
                )
                if ok:
                    mark_posted(video_id, "facebook")
                    logger.info(f"✅ Facebook retry success: {title[:60]}")
                    if ig_via_facebook:
                        mark_posted(video_id, "instagram")
                await asyncio.sleep(settings.POST_DELAY_SECONDS)
            
            # Telegram
            if tg_enabled and not row["posted_tg"]:
                ok = await telegram.post_video(file_path, title)
                if ok:
                    mark_posted(video_id, "telegram")
                    logger.info(f"✅ Telegram retry success: {title[:60]}")
            
            # Очистить ошибку если все успешно
            conn = sqlite3.connect(settings.DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM videos WHERE id = ?", (video_id,))
            updated = cursor.fetchone()
            conn.close()
            
            if updated:
                all_success = True
                if ig_enabled and not ig_via_facebook and not updated["posted_ig"]:
                    all_success = False
                if yt_enabled and not updated["posted_yt"]:
                    all_success = False
                if fb_enabled and not updated["posted_fb"]:
                    all_success = False
                if tg_enabled and not updated["posted_tg"]:
                    all_success = False
                
                if all_success:
                    mark_error(video_id, None)
                    logger.info(f"🎉 Video fully published: {title[:60]}")
            
            # Пауза между видео в retry — снижаем пиковую нагрузку на CPU
            await asyncio.sleep(settings.POST_DELAY_SECONDS)
    
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

    for video in new_videos:
        existing = get_video(video.id)
        if existing and all([
            not (get_setting("enable_instagram", "0") == "1") or existing.get("posted_ig"),
            not (get_setting("enable_youtube", "1") == "1") or existing.get("posted_yt"),
            not (get_setting("enable_facebook", "1") == "1") or existing.get("posted_fb"),
            not (get_setting("enable_telegram", "0") == "1") or existing.get("posted_tg"),
        ]):
            continue

        if not existing:
            save_video(video.id, video.title, video.file_path)
        logger.info(f"📹 Processing: {video.title[:60]}")

        errors = []
        posted_to = []

        # Instagram direct posting is skipped when Meta cross-post via Facebook is enabled.
        # This avoids Instagram API/IP blocks and keeps one publication path.
        ig_enabled = get_setting("enable_instagram", "0") == "1"
        fb_enabled = get_setting("enable_facebook", "1") == "1"
        ig_via_facebook = ig_enabled and fb_enabled

        if ig_enabled and not ig_via_facebook and not _posted(existing, "posted_ig"):
            ok = await instagram.post_reel(video.file_path, video.title)
            if ok:
                mark_posted(video.id, "instagram")
                posted_to.append("instagram")
            else:
                errors.append("Instagram")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # YouTube
        yt_enabled = get_setting("enable_youtube", "1") == "1"
        if yt_enabled and not _posted(existing, "posted_yt"):
            ok = await youtube.post_video(video.file_path, video.title, video.title)
            if ok:
                mark_posted(video.id, "youtube")
                posted_to.append("youtube")
            else:
                errors.append("YouTube")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # Facebook
        if fb_enabled and not _posted(existing, "posted_fb"):
            ok = await facebook.post_video(
                video.file_path,
                video.title,
                crosspost_to_instagram=ig_via_facebook,
            )
            if ok:
                mark_posted(video.id, "facebook")
                posted_to.append("facebook")
                if ig_via_facebook:
                    mark_posted(video.id, "instagram")
                    posted_to.append("instagram")
            else:
                errors.append("Facebook")
                if ig_via_facebook:
                    errors.append("Instagram(via Facebook)")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # Telegram
        tg_enabled = get_setting("enable_telegram", "0") == "1"
        if tg_enabled and not _posted(existing, "posted_tg"):
            ok = await telegram.post_video(video.file_path, video.title)
            if ok:
                mark_posted(video.id, "telegram")
                posted_to.append("telegram")
            else:
                errors.append("Telegram")

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
            for suffix in ("_yt.mp4", "_ig.mp4", "_fb.mp4"):
                tmp = base + suffix
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    # Сохраняем ID последнего видео
    if new_videos:
        set_setting("last_tiktok_id", new_videos[0].id)

    cleanup_old_media(keep_last=30)


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
