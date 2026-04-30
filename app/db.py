import sqlite3
import os
import logging
from config import settings

logger = logging.getLogger(__name__)

# Правильное отображение платформы -> колонка в БД
PLATFORM_COL = {
    "instagram": "posted_ig",
    "ig":        "posted_ig",
    "youtube":   "posted_yt",
    "yt":        "posted_yt",
    "facebook":  "posted_fb",
    "fb":        "posted_fb",
    "telegram":  "posted_tg",
    "tg":        "posted_tg",
}


def get_conn():
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tiktok_id   TEXT UNIQUE NOT NULL,
            title       TEXT,
            file_path   TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            posted_ig   INTEGER DEFAULT 0,
            posted_yt   INTEGER DEFAULT 0,
            posted_tg   INTEGER DEFAULT 0,
            posted_fb   INTEGER DEFAULT 0,
            error       TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # Миграция: добавить posted_fb если таблица уже существует без неё
        try:
            conn.execute("ALTER TABLE videos ADD COLUMN posted_fb INTEGER DEFAULT 0")
        except Exception:
            pass  # колонка уже есть
    logger.info("DB initialized")


def is_posted(tiktok_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM videos WHERE tiktok_id = ?", (tiktok_id,)
        ).fetchone()
    return row is not None


def save_video(tiktok_id: str, title: str, file_path: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO videos (tiktok_id, title, file_path) VALUES (?,?,?)",
            (tiktok_id, title, file_path)
        )


def mark_posted(tiktok_id: str, platform: str):
    col = PLATFORM_COL.get(platform.lower())
    if not col:
        logger.warning(f"mark_posted: unknown platform '{platform}'")
        return
    with get_conn() as conn:
        conn.execute(f"UPDATE videos SET {col} = 1 WHERE tiktok_id = ?", (tiktok_id,))


def mark_error(tiktok_id: str, error: str):
    with get_conn() as conn:
        conn.execute("UPDATE videos SET error = ? WHERE tiktok_id = ?", (error, tiktok_id))


def get_recent(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM videos ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
        )
