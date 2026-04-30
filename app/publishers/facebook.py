"""
Facebook publisher.
Использует Playwright с сохранённой сессией для публикации видео/Reels.
Пользователь логинится через встроенный браузер (noVNC) и сохраняет сессию.
"""
import os
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["facebook"]


async def login_facebook(username: str, password: str) -> dict:
    """Headless логин в Facebook, сохраняет storageState."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.facebook.com/login", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Принять cookies если появится
            for sel in ["button[data-cookiebanner='accept_button']", "[data-testid='cookie-policy-manage-dialog-accept-button']"]:
                try:
                    await page.click(sel, timeout=2000)
                except Exception:
                    pass

            await page.fill('#email', username)
            await page.fill('#pass', password)
            await page.click('[name="login"]')
            await page.wait_for_timeout(5000)

            url = page.url
            # 2FA / checkpoint
            if "checkpoint" in url or "two_step_verification" in url or "login/device-based" in url:
                return {"ok": False, "error": "2FA_REQUIRED"}

            # Ошибка пароля
            if "login" in url and "facebook.com/login" in url:
                return {"ok": False, "error": "Неверный логин или пароль"}

            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            await ctx.storage_state(path=SESSION)
            return {"ok": True}

        except PWTimeout:
            return {"ok": False, "error": "Таймаут входа в Facebook"}
        except Exception as e:
            logger.error(f"Facebook login: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            await browser.close()


async def login_facebook_2fa(username: str, password: str, code: str) -> dict:
    """Логин с подтверждением 2FA."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.facebook.com/login", wait_until="networkidle")
            await page.fill('#email', username)
            await page.fill('#pass', password)
            await page.click('[name="login"]')
            await page.wait_for_timeout(4000)

            # Ввод 2FA кода
            for sel in ['input[name="approvals_code"]', 'input[id*="approvals"]', 'input[type="text"]']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible():
                        await el.fill(code)
                        break
                except Exception:
                    pass

            # Нажать Continue / Submit
            for sel in ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Submit")', '#checkpointSubmitButton']:
                try:
                    await page.click(sel, timeout=3000)
                    break
                except Exception:
                    pass

            await page.wait_for_timeout(5000)

            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            await ctx.storage_state(path=SESSION)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await browser.close()


async def post_video(video_path: str, caption: str) -> bool:
    """Публикует видео/Reel на Facebook используя сохранённую сессию."""
    if not os.path.exists(SESSION):
        logger.error("Facebook: session not found")
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            storage_state=SESSION,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            # Пробуем через Reels Creator
            await page.goto("https://www.facebook.com/reels/create/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)

            # Если редирект на логин — сессия протухла
            if "login" in page.url:
                logger.error("Facebook: session expired")
                return False

            # Кликаем кнопку загрузки чтобы file input появился в DOM
            for sel in [
                'button:has-text("Select video")',
                'button:has-text("Выбрать видео")',
                'button:has-text("Select")',
                '[role="button"]:has-text("Upload")',
                '[aria-label*="Upload"]',
                '[aria-label*="upload"]',
            ]:
                try:
                    await page.click(sel, timeout=2000)
                    await page.wait_for_timeout(1000)
                    break
                except Exception:
                    pass

            # Напрямую устанавливаем файл в input[type="file"] (обходит file chooser)
            uploaded = False
            try:
                await page.wait_for_selector('input[type="file"]', state="attached", timeout=8000)
                await page.locator('input[type="file"]').first.set_input_files(video_path)
                uploaded = True
                logger.info("Facebook: video file set via Reels input")
            except Exception as e:
                logger.warning(f"Facebook Reels input failed: {e}")

            if not uploaded:
                return await _post_video_wall(page, video_path, caption)

            await page.wait_for_timeout(10000)

            # Описание
            for sel in [
                'div[contenteditable="true"]',
                'textarea',
                '[placeholder*="description"]',
                '[placeholder*="описание"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await page.keyboard.type(caption[:2000])
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

            # Публикация
            for sel in [
                'div[aria-label*="Publish"]',
                'div[aria-label*="Опубликовать"]',
                'button:has-text("Publish")',
                'button:has-text("Опубликовать")',
                'button:has-text("Share")',
            ]:
                try:
                    await page.click(sel, timeout=5000)
                    break
                except Exception:
                    pass

            await page.wait_for_timeout(10000)
            logger.info("Facebook: Reel published ✅")
            return True

        except Exception as e:
            logger.error(f"Facebook post error: {e}", exc_info=True)
            try:
                return await _post_video_wall(page, video_path, caption)
            except Exception:
                return False
        finally:
            await browser.close()


async def _post_video_wall(page, video_path: str, caption: str) -> bool:
    """Fallback: публикует видео обычным постом на стене."""
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if "login" in page.url:
            return False

        # Открыть диалог создания поста
        for sel in [
            '[aria-label="Create a post"]',
            '[placeholder*="mind"]',
            '[placeholder*="ум"]',
        ]:
            try:
                await page.click(sel, timeout=4000)
                await page.wait_for_timeout(2000)
                break
            except Exception:
                pass

        # Нажать кнопку "Photo/Video" в попапе
        for sel in [
            'div[role="button"]:has-text("Photo")',
            'div[role="button"]:has-text("Video")',
            '[aria-label*="Photo"]',
            '[aria-label*="Video"]',
            'span:has-text("Photo")',
            'span:has-text("Video")',
        ]:
            try:
                await page.click(sel, timeout=3000)
                await page.wait_for_timeout(2000)
                break
            except Exception:
                pass

        # Напрямую устанавливаем файл в input[type="file"]
        await page.wait_for_selector('input[type="file"]', state="attached", timeout=8000)
        await page.locator('input[type="file"]').first.set_input_files(video_path)
        await page.wait_for_timeout(10000)

        # Подпись
        for sel in ['div[contenteditable="true"]', 'textarea']:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await el.fill(caption[:2000])
                    break
            except Exception:
                pass

        # Опубликовать
        for sel in ['div[aria-label="Post"]', 'button:has-text("Post")', 'button[type="submit"]']:
            try:
                await page.click(sel, timeout=5000)
                break
            except Exception:
                pass

        await page.wait_for_timeout(8000)
        logger.info("Facebook: video posted via wall ✅")
        return True
    except Exception as e:
        logger.error(f"Facebook wall post error: {e}")
        return False
