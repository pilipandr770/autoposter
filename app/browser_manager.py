"""
BrowserManager запускает HEADED Chromium в Xvfb (виртуальный дисплей),
и отображает его в браузере заказчика через noVNC.

Заказчик открывает /vnc в браузере — видит живой Chromium — логинится сам.
После входа нажимает "Сохранить сессию" — мы сохраняем storageState.
"""

import asyncio
import glob
import logging
import os
import subprocess
import time
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)


def _find_chromium() -> str:
    """Finds Playwright Chromium binary path."""
    patterns = [
        "/ms-playwright/chromium-*/chrome-linux/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/home/**/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    # fallback
    for cmd in ("chromium", "chromium-browser", "google-chrome"):
        result = subprocess.run(["which", cmd], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    raise FileNotFoundError("Chromium not found. Check Playwright installation.")


class BrowserManager:
    VNC_PORT = 6080       # noVNC websocket порт (открытый снаружи)
    DISPLAY = ":99"       # виртуальный дисплей Xvfb

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._xvfb: subprocess.Popen | None = None
        self._x11vnc: subprocess.Popen | None = None
        self._novnc: subprocess.Popen | None = None
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    def start_display(self):
        """Запускает Xvfb + x11vnc + noVNC."""
        try:
            # Чистим stale lock-файлы от предыдущих запусков
            lock_file = f"/tmp/.X{self.DISPLAY.lstrip(':')}-lock"
            if os.path.exists(lock_file):
                os.remove(lock_file)
                logger.info(f"Removed stale lock: {lock_file}")
            socket_file = f"/tmp/.X11-unix/X{self.DISPLAY.lstrip(':')}"
            if os.path.exists(socket_file):
                os.remove(socket_file)
            # Убиваем старые процессы
            subprocess.run(["pkill", "-9", "-f", "Xvfb"], capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "x11vnc"], capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "websockify"], capture_output=True)
            time.sleep(0.5)

            # Xvfb — виртуальный экран
            self._xvfb = subprocess.Popen(
                ["Xvfb", self.DISPLAY, "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(1.5)
            # Проверяем что Xvfb запустился
            if self._xvfb.poll() is not None:
                raise RuntimeError(f"Xvfb exited immediately (code {self._xvfb.returncode})")

            # x11vnc — VNC сервер поверх Xvfb
            self._x11vnc = subprocess.Popen(
                ["x11vnc", "-display", self.DISPLAY, "-forever",
                 "-nopw", "-quiet", "-rfbport", "5900"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(1)

            # noVNC — веб-клиент для VNC
            novnc_path = "/usr/share/novnc"
            if not os.path.exists(novnc_path):
                novnc_path = "/usr/local/share/novnc"

            self._novnc = subprocess.Popen(
                ["websockify", "--web", novnc_path,
                 str(self.VNC_PORT), "127.0.0.1:5900"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            time.sleep(0.5)

            self._ready = True
            logger.info(f"Display ready. noVNC on port {self.VNC_PORT}")
        except Exception as e:
            logger.error(f"Failed to start display: {e}")
            self._ready = False

    def open_url(self, url: str) -> bool:
        """Открывает URL в видимом браузере."""
        if not self._ready:
            self.start_display()

        if self._page is None:
            # Запускаем синхронно через subprocess для простоты
            os.environ["DISPLAY"] = self.DISPLAY
            chromium = _find_chromium()
            subprocess.Popen([
                chromium, "--no-sandbox", "--disable-dev-shm-usage",
                "--start-maximized", url
            ], env={**os.environ, "DISPLAY": self.DISPLAY})
            logger.info(f"Opened browser: {url}")
            return True
        return True

    async def save_session(self, session_path: str) -> bool:
        """
        Сохраняет cookies из запущенного браузера через CDP.
        Подключается к уже открытому Chromium через remote debugging.
        """
        try:
            async with async_playwright() as p:
                # Подключаемся к уже запущенному браузеру
                browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                contexts = browser.contexts
                if not contexts:
                    return False

                ctx = contexts[0]
                os.makedirs(os.path.dirname(session_path), exist_ok=True)
                await ctx.storage_state(path=session_path)
                logger.info(f"Session saved: {session_path}")
                return True
        except Exception as e:
            logger.error(f"Save session error: {e}")
            return False

    def open_url_with_cdp(self, url: str) -> bool:
        """Открывает Chromium с CDP + в Xvfb."""
        if not self._ready:
            self.start_display()

        # Убиваем предыдущий если есть
        self.stop_browser()

        chromium = _find_chromium()
        env = {**os.environ, "DISPLAY": self.DISPLAY}
        subprocess.Popen([
            chromium,
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--remote-debugging-port=9222",
            "--remote-debugging-address=0.0.0.0",
            "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            url
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(2)
        logger.info(f"Browser opened with CDP: {url}")
        return True

    def stop_browser(self):
        """Закрывает Chromium."""
        subprocess.run(["pkill", "-f", "chrome-linux/chrome"], capture_output=True)
        subprocess.run(["pkill", "-f", "chromium"], capture_output=True)

    def stop(self):
        """Останавливает всё."""
        self.stop_browser()
        for proc in [self._novnc, self._x11vnc, self._xvfb]:
            if proc:
                proc.terminate()
        self._ready = False
