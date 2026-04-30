import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Telegram Bot (для публикации в канал и мониторинга)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHANNEL_ID: str = ""  # @channel_name или -100xxxxxxxxxx

    # TikTok аккаунт для мониторинга
    TIKTOK_USERNAME: str = ""       # без @

    # Интервал проверки TikTok (минуты)
    CHECK_INTERVAL_MINUTES: int = 30

    # Задержка между постами на платформы (секунды)
    POST_DELAY_SECONDS: int = 15

    # Пути
    DATA_DIR: str = "/app/data"
    SESSIONS_DIR: str = "/app/data/sessions"
    MEDIA_DIR: str = "/app/data/media"
    DB_PATH: str = "/app/data/db/autoposter.db"

    # Web UI
    WEB_SECRET_KEY: str = "change_me_in_production_please"
    WEB_PORT: int = 5000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()

# Пути к файлам сессий (Playwright storageState)
SESSION_FILES = {
    "instagram": f"{settings.SESSIONS_DIR}/instagram.json",
    "youtube":   f"{settings.SESSIONS_DIR}/youtube.json",
    "facebook":  f"{settings.SESSIONS_DIR}/facebook.json",
}

# Настройки платформ
PLATFORMS = {
    "instagram": {"name": "Instagram", "icon": "📸", "enabled": True},
    "youtube":   {"name": "YouTube",   "icon": "▶️",  "enabled": True},
    "facebook":  {"name": "Facebook",  "icon": "📘",  "enabled": True},
    "telegram":  {"name": "Telegram",  "icon": "✈️",  "enabled": True},
}
