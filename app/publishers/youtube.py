import os
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["youtube"]


async def login_youtube(username: str, password: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://accounts.google.com/signin/v2/identifier", wait_until="networkidle")
            await page.wait_for_timeout(1500)

            await page.fill('input[type="email"]', username)
            await page.click("#identifierNext")
            await page.wait_for_timeout(2500)

            await page.fill('input[type="password"]', password)
            await page.click("#passwordNext")
            await page.wait_for_timeout(5000)

            # Ошибка
            err = page.locator('[data-error-code], .o6cuMc, [jsname="B34EJ"]')
            if await err.count() > 0:
                return {"ok": False, "error": "Неверный логин или пароль Google"}

            # 2FA check
            url = page.url
            if "challenge" in url or "signin/v2/challenge" in url:
                return {"ok": False, "error": "2FA_REQUIRED"}

            # Переходим в Studio
            await page.goto("https://studio.youtube.com", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            if "studio.youtube.com" not in page.url:
                return {"ok": False, "error": "Не удалось перейти в YouTube Studio. Возможно нужна 2FA."}

            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            await ctx.storage_state(path=SESSION)
            return {"ok": True}

        except PWTimeout:
            return {"ok": False, "error": "Таймаут входа в Google"}
        except Exception as e:
            logger.error(f"YouTube login: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            await browser.close()


async def post_video(video_path: str, title: str, description: str) -> bool:
    if not os.path.exists(SESSION):
        logger.error("YouTube: session not found")
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            storage_state=SESSION,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Проверяем что залогинены
            if "accounts.google.com" in page.url or "signin" in page.url:
                logger.error("YouTube: session expired or not logged in")
                return False

            # Кнопка CREATE (разные варианты)
            for sel in [
                'ytcp-button#create-icon',
                'button[aria-label="CREATE"]',
                'button[aria-label="Создать"]',
                '#create-icon',
                'ytcp-icon-button.ytcp-create-icon-renderer',
                '[test-id="create-icon"]',
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    break
                except Exception:
                    pass
            await page.wait_for_timeout(1500)

            # Upload videos
            for txt in ["Upload video", "Загрузить видео", "Upload"]:
                try:
                    await page.click(f'tp-yt-paper-item:has-text("{txt}")', timeout=2000)
                    break
                except Exception:
                    pass
            await page.wait_for_timeout(2000)

            # Выбрать файл — напрямую через input[type="file"]
            try:
                await page.wait_for_selector('input[type="file"]', state="attached", timeout=10000)
                await page.locator('input[type="file"]').first.set_input_files(video_path)
            except Exception:
                # Fallback через file chooser
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    for sel in [
                        'ytcp-uploads-file-picker #select-files-button',
                        '[class*="file-picker"] button',
                        'button:has-text("SELECT FILES")',
                        'button:has-text("ВЫБРАТЬ ФАЙЛЫ")',
                    ]:
                        try:
                            await page.click(sel, timeout=2000)
                            break
                        except Exception:
                            pass
                fc = await fc_info.value
                await fc.set_files(video_path)

            # Ждём появления формы редактирования
            await page.wait_for_selector('#textbox', timeout=60000)
            await page.wait_for_timeout(3000)

            # Заголовок
            title_box = page.locator('#textbox').first
            await title_box.triple_click()
            await title_box.type(title[:100], delay=30)

            # Описание
            desc_box = page.locator('#textbox').nth(1)
            await desc_box.click()
            await desc_box.type(description[:4500], delay=10)

            await page.wait_for_timeout(1000)

            # Не для детей
            try:
                radio = page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]')
                if await radio.is_visible():
                    await radio.click()
            except Exception:
                pass

            # Next x3
            for i in range(3):
                next_btn = page.locator('#next-button, ytcp-button#next-button')
                if await next_btn.is_visible():
                    await next_btn.click()
                    await page.wait_for_timeout(2500)

            # Public
            try:
                public = page.locator('tp-yt-paper-radio-button[name="PUBLIC"]')
                if await public.is_visible():
                    await public.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Publish / Done
            done_btn = page.locator('#done-button, ytcp-button#done-button')
            await done_btn.click()
            await page.wait_for_timeout(8000)

            logger.info("YouTube: video published ✅")
            return True

        except Exception as e:
            logger.error(f"YouTube post error: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
