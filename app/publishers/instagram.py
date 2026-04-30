import os
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["instagram"]


async def login_instagram(username: str, password: str) -> dict:
    """Headless логин с поддержкой 2FA."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Cookies popup
            for selector in ["text=Allow all cookies", "text=Alle Cookies erlauben", "text=Принять все"]:
                try:
                    await page.click(selector, timeout=2000)
                    break
                except Exception:
                    pass

            await page.fill('input[name="username"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(5000)

            # Ошибка входа
            if await page.locator("#slfErrorAlert").count() > 0:
                return {"ok": False, "error": "Неверный логин или пароль"}

            # 2FA
            if await page.locator('input[name="verificationCode"]').count() > 0:
                return {"ok": False, "error": "2FA_REQUIRED"}

            # Попапы "Save info / Turn on notifications"
            for txt in ["Not Now", "Не сейчас", "Save Info"]:
                try:
                    await page.click(f"text={txt}", timeout=3000)
                except Exception:
                    pass

            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            await ctx.storage_state(path=SESSION)
            return {"ok": True}

        except PWTimeout:
            return {"ok": False, "error": "Таймаут. Возможно Instagram требует верификацию."}
        except Exception as e:
            logger.error(f"Instagram login: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            await browser.close()


async def login_instagram_2fa(username: str, password: str, code: str) -> dict:
    """Логин с 2FA кодом."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle")
            await page.fill('input[name="username"]', username)
            await page.fill('input[name="password"]', password)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

            await page.fill('input[name="verificationCode"]', code)
            await page.click('button[type="button"]:has-text("Confirm"), button:has-text("Submit")')
            await page.wait_for_timeout(4000)

            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            await ctx.storage_state(path=SESSION)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            await browser.close()


async def post_reel(video_path: str, caption: str) -> bool:
    """Публикует Reel в Instagram."""
    if not os.path.exists(SESSION):
        logger.error("Instagram: session not found")
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            storage_state=SESSION,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.instagram.com/", wait_until="networkidle")
            await page.wait_for_timeout(2000)

            # Открыть диалог создания
            await page.click('[aria-label="New post"], [aria-label="Создать"]')
            await page.wait_for_timeout(1500)

            # Выбрать файл
            async with page.expect_file_chooser() as fc:
                await page.click("text=Select from computer, text=Выбрать с компьютера, [accept]")
            fc_val = await fc.value
            await fc_val.set_files(video_path)
            await page.wait_for_timeout(6000)

            # OK на предупреждение о качестве
            for txt in ["OK", "ОК"]:
                try:
                    await page.click(f"button:has-text('{txt}')", timeout=2000)
                except Exception:
                    pass

            # Next x2
            for _ in range(2):
                for sel in ["button:has-text('Next')", "button:has-text('Далее')"]:
                    try:
                        await page.click(sel, timeout=3000)
                        await page.wait_for_timeout(2000)
                        break
                    except Exception:
                        pass

            # Caption
            for sel in ["div[contenteditable='true']", "textarea"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible():
                        await el.click()
                        await el.fill(caption[:2200])
                        break
                except Exception:
                    pass

            # Share
            for sel in ["button:has-text('Share')", "button:has-text('Поделиться')"]:
                try:
                    await page.click(sel, timeout=5000)
                    break
                except Exception:
                    pass

            await page.wait_for_timeout(8000)
            logger.info("Instagram: Reel published ✅")
            return True

        except Exception as e:
            logger.error(f"Instagram post error: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
