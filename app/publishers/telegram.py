import logging
from aiogram import Bot
from aiogram.types import FSInputFile
from config import settings
from app.db import get_setting

logger = logging.getLogger(__name__)


def _get_token() -> str:
    return get_setting("telegram_token") or settings.TELEGRAM_BOT_TOKEN


def _get_channel() -> str:
    return get_setting("telegram_channel") or settings.TELEGRAM_CHANNEL_ID


async def post_video(video_path: str, caption: str) -> bool:
    token = _get_token()
    if not token:
        logger.warning("Telegram: bot token not configured, skipping")
        return False

    channel = _get_channel()
    if not channel:
        logger.warning("Telegram: channel ID not configured, skipping")
        return False

    bot = Bot(token=token)
    try:
        video_file = FSInputFile(video_path)
        await bot.send_video(
            chat_id=channel,
            video=video_file,
            caption=caption[:1024],
            supports_streaming=True
        )
        logger.info("Telegram: video posted ✅")
        return True
    except Exception as e:
        logger.error(f"Telegram post error: {e}")
        return False
    finally:
        await bot.session.close()
