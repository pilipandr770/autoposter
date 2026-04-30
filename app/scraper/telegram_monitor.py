"""
Telegram Channel Monitor.
Слушает новые посты в Telegram канале (через бота).
Видео из канала → публикуются в Instagram и Facebook.

Требования:
- Бот добавлен в канал как администратор (или участник)
- В настройках задан TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from app.db import is_posted, save_video, mark_posted, mark_error, get_setting
from app.publishers import instagram, facebook
from config import settings

logger = logging.getLogger(__name__)


async def _download_tg_video(bot: Bot, file_id: str, dest_dir: str) -> str | None:
    """Скачивает видео из Telegram по file_id."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"tg_{file_id[:24]}.mp4")
        if os.path.exists(dest):
            return dest
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, dest)
        logger.info(f"Telegram: downloaded {dest}")
        return dest
    except Exception as e:
        logger.error(f"Telegram download error: {e}")
        return None


async def _handle_channel_post(message: Message, bot: Bot):
    """Обрабатывает новый пост из Telegram канала."""
    # Принимаем только видео
    video = message.video or (
        message.document
        if message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
        else None
    )
    if not video:
        return

    tg_post_id = f"tg_{message.message_id}"
    if is_posted(tg_post_id):
        return

    ig_enabled = get_setting("enable_tg_to_ig", "1") == "1"
    fb_enabled = get_setting("enable_tg_to_fb", "1") == "1"

    if not ig_enabled and not fb_enabled:
        logger.info("Telegram→Instagram and Telegram→Facebook both disabled, skipping")
        return

    caption = message.caption or ""
    logger.info(f"Telegram monitor: new video post #{message.message_id}")

    file_path = await _download_tg_video(bot, video.file_id, settings.MEDIA_DIR)
    if not file_path:
        logger.error(f"Telegram monitor: failed to download post #{message.message_id}")
        return

    save_video(tg_post_id, caption[:200], file_path)
    errors = []

    # Telegram → Instagram
    if ig_enabled:
        ok = await instagram.post_reel(file_path, caption)
        if ok:
            mark_posted(tg_post_id, "instagram")
            logger.info(f"Telegram→Instagram: published post #{message.message_id} ✅")
        else:
            errors.append("Instagram")
            logger.error(f"Telegram→Instagram: failed post #{message.message_id}")
        await asyncio.sleep(settings.POST_DELAY_SECONDS)

    # Telegram → Facebook
    if fb_enabled:
        ok = await facebook.post_video(file_path, caption)
        if ok:
            mark_posted(tg_post_id, "facebook")
            logger.info(f"Telegram→Facebook: published post #{message.message_id} ✅")
        else:
            errors.append("Facebook")
            logger.error(f"Telegram→Facebook: failed post #{message.message_id}")

    if errors:
        mark_error(tg_post_id, f"Failed: {', '.join(errors)}")

    # Удаляем медиафайл после отправки
    if file_path and os.path.exists(file_path) and (ig_enabled or fb_enabled):
        try:
            os.remove(file_path)
            logger.info(f"🗑️ Deleted TG media after posting: {file_path}")
        except Exception as e:
            logger.warning(f"Could not delete TG media file: {e}")


async def start_telegram_monitor():
    """Запускает мониторинг Telegram канала. Безопасно завершается если токен не задан."""
    token = get_setting("telegram_token") or settings.TELEGRAM_BOT_TOKEN
    if not token:
        logger.info("Telegram monitor: no bot token configured, skipping")
        return

    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.channel_post()
    async def on_channel_post(message: Message):
        try:
            await _handle_channel_post(message, bot)
        except Exception as e:
            logger.error(f"Telegram monitor handler error: {e}", exc_info=True)

    logger.info("✅ Telegram monitor started (polling)")
    try:
        await dp.start_polling(bot, handle_signals=False)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Telegram monitor stopped: {e}")
    finally:
        await bot.session.close()


async def start_monitor_safe():
    """Обёртка — перезапускает монитор при падении."""
    while True:
        try:
            await start_telegram_monitor()
        except Exception as e:
            logger.error(f"Telegram monitor crash, restarting in 60s: {e}")
        await asyncio.sleep(60)
