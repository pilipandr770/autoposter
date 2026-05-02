import asyncio
import logging
import os
from app.db import (
    is_posted, save_video, mark_posted, mark_error,
    get_setting, set_setting
)
from app.scraper.tiktok import get_new_videos, cleanup_old_media
from app.publishers import instagram, youtube, telegram, facebook
from config import settings

logger = logging.getLogger(__name__)

_running = False
_checking = False  # prevent concurrent check_and_post() calls


async def check_and_post():
    """Главный цикл: проверяем TikTok → постим новые видео на все платформы."""
    global _checking
    if _checking:
        logger.warning("Check already in progress, skipping duplicate call")
        return
    _checking = True
    try:
        await _do_check_and_post()
    finally:
        _checking = False


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
        if is_posted(video.id):
            continue

        save_video(video.id, video.title, video.file_path)
        logger.info(f"📹 Processing: {video.title[:60]}")

        errors = []
        posted_to = []

        # Instagram
        ig_enabled = get_setting("enable_instagram", "1") == "1"
        if ig_enabled:
            ok = await instagram.post_reel(video.file_path, video.title)
            if ok:
                mark_posted(video.id, "instagram")
                posted_to.append("instagram")
            else:
                errors.append("Instagram")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # YouTube
        yt_enabled = get_setting("enable_youtube", "1") == "1"
        if yt_enabled:
            ok = await youtube.post_video(video.file_path, video.title, video.title)
            if ok:
                mark_posted(video.id, "youtube")
                posted_to.append("youtube")
            else:
                errors.append("YouTube")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # Facebook
        fb_enabled = get_setting("enable_facebook", "1") == "1"
        if fb_enabled:
            ok = await facebook.post_video(video.file_path, video.title)
            if ok:
                mark_posted(video.id, "facebook")
                posted_to.append("facebook")
            else:
                errors.append("Facebook")
            await asyncio.sleep(settings.POST_DELAY_SECONDS)

        # Telegram
        tg_enabled = get_setting("enable_telegram", "1") == "1"
        if tg_enabled:
            ok = await telegram.post_video(video.file_path, video.title)
            if ok:
                mark_posted(video.id, "telegram")
                posted_to.append("telegram")
            else:
                errors.append("Telegram")

        if errors:
            mark_error(video.id, f"Failed: {', '.join(errors)}")

        # Удаляем медиафайл после отправки (не хранить лишнее)
        if posted_to and os.path.exists(video.file_path):
            try:
                os.remove(video.file_path)
                logger.info(f"🗑️ Deleted media after posting: {video.file_path}")
            except Exception as e:
                logger.warning(f"Could not delete media file: {e}")

    # Сохраняем ID последнего видео
    if new_videos:
        set_setting("last_tiktok_id", new_videos[0].id)

    cleanup_old_media(keep_last=30)


async def scheduler_loop():
    global _running
    _running = True
    interval = settings.CHECK_INTERVAL_MINUTES * 60

    logger.info(f"⏰ Scheduler started. Interval: {settings.CHECK_INTERVAL_MINUTES} min")

    while _running:
        try:
            await check_and_post()
        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
        await asyncio.sleep(interval)


def stop_scheduler():
    global _running
    _running = False
