import asyncio
import threading
import logging
from app.db import init_db
from app.web.app import create_flask_app
from app.scheduler import scheduler_loop
from app.browser_manager import BrowserManager
from app.scraper.telegram_monitor import start_monitor_safe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def run_flask(browser_mgr: BrowserManager):
    app = create_flask_app()
    app.config["BROWSER_MGR"] = browser_mgr
    from config import settings
    app.run(host="0.0.0.0", port=settings.WEB_PORT, debug=False, use_reloader=False)


async def main():
    # Инициализация БД
    init_db()
    logger.info("✅ DB initialized")

    # Запуск виртуального дисплея (Xvfb + noVNC) для встроенного браузера
    browser_mgr = BrowserManager()
    browser_mgr.start_display()

    # Flask в отдельном потоке
    flask_thread = threading.Thread(
        target=run_flask, args=(browser_mgr,), daemon=True
    )
    flask_thread.start()
    logger.info("✅ Web UI started on port 5000")

    # Telegram монитор канала → Instagram (фоновая задача)
    asyncio.create_task(start_monitor_safe())
    logger.info("✅ Telegram monitor task created")

    # Основной планировщик (TikTok → Instagram/YouTube/Facebook/Telegram)
    logger.info("✅ Starting scheduler...")
    await scheduler_loop()


if __name__ == "__main__":
    asyncio.run(main())
