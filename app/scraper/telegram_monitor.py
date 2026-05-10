"""
Telegram Channel Monitor.
Слушает новые посты в Telegram канале (через бота).
Видео и фото из канала → публикуются в Instagram и Facebook.

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


async def _download_tg_file(bot: Bot, file_id: str, dest_dir: str, ext: str = "mp4") -> str | None:
    """Скачивает файл из Telegram по file_id."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"tg_{file_id[:24]}.{ext}")
        if os.path.exists(dest):
            return dest
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, dest)
        logger.info(f"Telegram: downloaded {dest}")
        return dest
    except Exception as e:
        logger.error(f"Telegram download error: {e}")
        return None


def _check_channel(message: Message, allowed_channel_id: str) -> bool:
    """Возвращает True если сообщение из разрешённого канала."""
    if not allowed_channel_id:
        return True
    chat_id = str(message.chat.id)
    if allowed_channel_id.startswith("@"):
        chat_username = f"@{message.chat.username}" if message.chat.username else ""
        return chat_username.lower() == allowed_channel_id.lower()
    return chat_id == allowed_channel_id


async def _process_media_post(
    message: Message,
    bot: Bot,
    file_id: str,
    file_ext: str,
    caption: str,
    post_label: str,
):
    """Общая логика обработки поста с медиа (видео или фото)."""
    tg_post_id = f"tg_{message.message_id}"
    if is_posted(tg_post_id):
        return

    ig_enabled = get_setting("enable_tg_to_ig", "0") == "1"
    fb_enabled = get_setting("enable_tg_to_fb", "1") == "1"

    if not ig_enabled and not fb_enabled:
        logger.info("Telegram→Instagram and Telegram→Facebook both disabled, skipping")
        return

    logger.info(f"Telegram monitor: new {post_label} post #{message.message_id}, caption={caption[:60]!r}")

    file_path = await _download_tg_file(bot, file_id, settings.MEDIA_DIR, ext=file_ext)
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
        base = file_path.rsplit(".", 1)[0]
        for suffix in ("_fb.mp4", "_ig.mp4"):
            tmp = base + suffix
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


async def _handle_channel_post(message: Message, bot: Bot, allowed_channel_id: str):
    """Обрабатывает новый пост из Telegram канала (видео или фото)."""
    if not _check_channel(message, allowed_channel_id):
        return

    caption = message.caption or message.text or ""

    # Видео (прямое или документ с video/* MIME)
    if message.video:
        await _process_media_post(
            message, bot,
            file_id=message.video.file_id,
            file_ext="mp4",
            caption=caption,
            post_label="video",
        )
        return

    if (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("video/")
    ):
        await _process_media_post(
            message, bot,
            file_id=message.document.file_id,
            file_ext="mp4",
            caption=caption,
            post_label="video-document",
        )
        return

    # Фото (берём наибольшее по размеру)
    if message.photo:
        best = max(message.photo, key=lambda p: p.file_size or 0)
        await _process_media_post(
            message, bot,
            file_id=best.file_id,
            file_ext="jpg",
            caption=caption,
            post_label="photo",
        )
        return


async def start_telegram_monitor():
    """Запускает мониторинг Telegram канала. Безопасно завершается если токен не задан."""
    token = get_setting("telegram_token") or settings.TELEGRAM_BOT_TOKEN
    if not token:
        logger.info("Telegram monitor: no bot token configured, skipping")
        return

    channel_id = (get_setting("telegram_channel") or settings.TELEGRAM_CHANNEL_ID).strip()
    if not channel_id:
        logger.warning("Telegram monitor: TELEGRAM_CHANNEL_ID not set — bot will listen to ALL channels!")
    else:
        logger.info(f"Telegram monitor: filtering posts from channel {channel_id}")

    bot = Bot(token=token)
    dp = Dispatcher()

    @dp.channel_post()
    async def on_channel_post(message: Message):
        try:
            await _handle_channel_post(message, bot, channel_id)
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
