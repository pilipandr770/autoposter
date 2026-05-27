import asyncio
import os
import logging
import threading
from flask import Flask, render_template, request, jsonify
from config import settings, SESSION_FILES
from app.db import (
    get_recent, get_setting, set_setting,
)
from app.browser_manager import BrowserManager

logger = logging.getLogger(__name__)

browser_mgr = BrowserManager()

# Хранилище pending 2FA flows
_pending_2fa: dict = {}


def create_flask_app():
    app = Flask(__name__)
    app.secret_key = settings.WEB_SECRET_KEY

    os.makedirs(settings.SESSIONS_DIR, exist_ok=True)

    def run_async(coro):
        """Запускает async функцию в новом event loop (из Flask thread)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _youtube_session_valid() -> bool:
        """Returns True if youtube_oauth.json exists with a refresh_token."""
        import json
        path = os.path.join(settings.SESSIONS_DIR, "youtube_oauth.json")
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            return bool(data.get("refresh_token"))
        except Exception:
            return False

    def platform_status():
        return {
            "youtube":  _youtube_session_valid(),
            "telegram": bool(get_setting("telegram_token") or settings.TELEGRAM_BOT_TOKEN),
        }

    # ── Страницы ──────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        st = platform_status()
        recent = get_recent(15)
        cfg = {
            "tiktok_user":      get_setting("tiktok_username") or settings.TIKTOK_USERNAME,
            "interval":         get_setting("check_interval") or str(settings.CHECK_INTERVAL_MINUTES),
            "tg_token":         get_setting("telegram_token") or settings.TELEGRAM_BOT_TOKEN,
            "tg_channel":       get_setting("telegram_channel") or settings.TELEGRAM_CHANNEL_ID,
            "enable_youtube":   get_setting("enable_youtube", "1") == "1",
            "enable_telegram":  get_setting("enable_telegram", "0") == "1",
            "enable_tg_to_yt":  get_setting("enable_tg_to_yt", "1") == "1",
        }
        vnc_ready = browser_mgr.is_ready()
        return render_template(
            "index.html",
            status=st,
            recent=recent,
            cfg=cfg,
            vnc_ready=vnc_ready,
            vnc_port=browser_mgr.VNC_PORT,
        )

    # ── API: Настройки ────────────────────────────────────────────────────
    @app.route("/api/settings", methods=["POST"])
    def save_settings():
        data = request.json or {}
        if "tiktok_username" in data:
            set_setting("tiktok_username", data["tiktok_username"].lstrip("@").strip())
        if "telegram_token" in data:
            set_setting("telegram_token", data["telegram_token"].strip())
        if "telegram_channel" in data:
            set_setting("telegram_channel", data["telegram_channel"].strip())
        if "check_interval" in data:
            set_setting("check_interval", str(data["check_interval"]))
        if "enable_youtube" in data:
            set_setting("enable_youtube", "1" if data["enable_youtube"] else "0")
        if "enable_telegram" in data:
            set_setting("enable_telegram", "1" if data["enable_telegram"] else "0")
        if "enable_tg_to_yt" in data:
            set_setting("enable_tg_to_yt", "1" if data["enable_tg_to_yt"] else "0")
        return jsonify({"ok": True})

    # ── API: Статус платформ ──────────────────────────────────────────────
    @app.route("/api/status")
    def api_status():
        return jsonify(platform_status())

    # ── API: Подключение YouTube ──────────────────────────────────────────
    @app.route("/api/connect/youtube", methods=["POST"])
    def connect_youtube():
        return jsonify({"ok": False, "error": "YouTube uses OAuth token file. No login needed."})

    # ── API: Подключение Telegram ─────────────────────────────────────────
    @app.route("/api/connect/telegram", methods=["POST"])
    def connect_telegram():
        import urllib.request as _urlreq
        import json as _json
        data = request.json or {}
        token = data.get("token", "").strip()
        channel = data.get("channel", "").strip()
        if not token or not channel:
            return jsonify({"ok": False, "error": "Укажите токен бота и ID канала"})
        # Проверяем токен через Telegram Bot API
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            req = _urlreq.Request(url, headers={"User-Agent": "autoposter/1.0"})
            with _urlreq.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
            if not result.get("ok"):
                return jsonify({"ok": False, "error": "Неверный токен бота"})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Не удалось проверить токен: {e}"})
        # Сохраняем в БД
        set_setting("telegram_token", token)
        set_setting("telegram_channel", channel)
        bot_username = result["result"].get("username", "")
        return jsonify({"ok": True, "bot": bot_username})

    # ── API: Отключение ───────────────────────────────────────────────────
    @app.route("/api/disconnect/<platform>", methods=["POST"])
    def disconnect(platform):
        # Для браузерных платформ — удаляем файл сессии
        path = SESSION_FILES.get(platform)
        if path and os.path.exists(path):
            os.remove(path)
        # Для Telegram — очищаем токен и channel из настроек БД
        if platform == "telegram":
            set_setting("telegram_token", "")
            set_setting("telegram_channel", "")
        return jsonify({"ok": True})

    # ── API: Сброс тестовых данных ────────────────────────────────────────
    @app.route("/api/reset", methods=["POST"])
    def reset_all():
        """
        Полный сброс: удаляет все сессии, очищает историю в БД,
        удаляет скачанные медиафайлы. Использовать перед передачей заказчику.
        """
        import glob
        removed = {"sessions": 0, "media": 0, "db_records": 0}

        # 1. Все файлы сессий
        for path in SESSION_FILES.values():
            if os.path.exists(path):
                os.remove(path)
                removed["sessions"] += 1

        # 2. Все скачанные медиафайлы
        media_dir = settings.MEDIA_DIR
        if os.path.isdir(media_dir):
            for f in glob.glob(os.path.join(media_dir, "*")):
                try:
                    os.remove(f)
                    removed["media"] += 1
                except Exception:
                    pass

        # 3. Очищаем таблицу videos и настройки (токены, TikTok username)
        from app.db import get_conn
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM videos")
            removed["db_records"] = cur.rowcount
            # Очищаем сохранённые токены и аккаунты
            for key in ("telegram_token", "telegram_channel", "last_tiktok_id",
                        "tiktok_username"):
                conn.execute("DELETE FROM settings WHERE key=?", (key,))

        # 4. Также очищаем pending 2FA
        _pending_2fa.clear()

        return jsonify({"ok": True, "removed": removed})

    # ── API: Встроенный браузер (noVNC) ───────────────────────────────────
    @app.route("/api/browser/open/<platform>", methods=["POST"])
    def browser_open(platform):
        urls = {
            "instagram": "https://www.instagram.com/accounts/login/",
            "youtube":   "https://accounts.google.com/signin",
            "facebook":  "https://www.facebook.com/login",
            "tiktok":    "https://www.tiktok.com/login",
        }
        url = urls.get(platform)
        if not url:
            return jsonify({"ok": False, "error": "Unknown platform"})
        ok = browser_mgr.open_url_with_cdp(url)
        return jsonify({"ok": ok})

    @app.route("/api/browser/save/<platform>", methods=["POST"])
    def browser_save(platform):
        session_path = SESSION_FILES.get(platform)
        if not session_path:
            return jsonify({"ok": False, "error": "Unknown platform"})
        ok = run_async(browser_mgr.save_session(session_path))
        # Kill the browser after saving — prevents runaway Chrome processes
        browser_mgr.stop_browser()
        return jsonify({"ok": ok})

    @app.route("/api/browser/stop", methods=["POST"])
    def browser_stop():
        browser_mgr.stop()
        return jsonify({"ok": True})

    # ── API: История постов ───────────────────────────────────────────────
    @app.route("/api/history")
    def api_history():
        return jsonify(get_recent(20))

    # ── API: Ручной запуск проверки ───────────────────────────────────────
    @app.route("/api/check-now", methods=["POST"])
    def check_now():
        from app.scheduler import check_and_post
        def _run():
            run_async(check_and_post())
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "message": "Проверка запущена"})

    return app
