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
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            storage_state=SESSION,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Проверяем что залогинены
            if "login" in page.url or await page.locator('input[name="username"]').count() > 0:
                logger.error("Instagram: session expired or not logged in")
                return False

            # Попробовать несколько вариантов кнопки "Создать"
            create_clicked = False
            for sel in [
                '[aria-label="New post"]',
                '[aria-label="Создать"]',
                '[aria-label="Create"]',
                'svg[aria-label="New post"]',
                'a[href="/create/select/"]',
                'a[href*="create"]',
                'span:has-text("Create")',
                'span:has-text("Создать")',
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    create_clicked = True
                    logger.info(f"Instagram: clicked create with: {sel}")
                    break
                except Exception:
                    pass

            if not create_clicked:
                # Прямая навигация на страницу создания
                logger.info("Instagram: navigating directly to /create/select/")
                await page.goto("https://www.instagram.com/create/select/", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)

            # Выбрать файл через file chooser
            try:
                async with page.expect_file_chooser(timeout=15000) as fc_info:
                    for sel in [
                        "text=Select from computer",
                        "text=Выбрать с компьютера",
                        "text=Select from Device",
                        "button:has-text('Select')",
                        "input[type='file']",
                    ]:
                        try:
                            await page.click(sel, timeout=3000)
                            break
                        except Exception:
                            pass
                file_chooser = await fc_info.value
                await file_chooser.set_files(video_path)
                logger.info("Instagram: file selected via file chooser")
            except Exception as e:
                # Попробуем напрямую через hidden input
                logger.warning(f"File chooser failed ({e}), trying direct input")
                try:
                    await page.wait_for_selector('input[type="file"]', state="attached", timeout=8000)
                    await page.locator('input[type="file"]').first.set_input_files(video_path)
                    logger.info("Instagram: file set via hidden input")
                except Exception as e2:
                    logger.error(f"Instagram: file upload failed: {e2}")
                    return False

            await page.wait_for_timeout(8000)

            # OK на предупреждение о качестве/соотношении сторон
            for txt in ["OK", "ОК", "Crop", "Select Crop"]:
                try:
                    await page.click(f"button:has-text('{txt}')", timeout=2000)
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass

            # Next / Далее — до 3 раз, с паузами
            for step in range(3):
                clicked_next = False
                for sel in [
                    "button:has-text('Next')",
                    "button:has-text('Далее')",
                    "div[role='button']:has-text('Next')",
                    "div[role='button']:has-text('Далее')",
                ]:
                    try:
                        await page.wait_for_selector(sel, timeout=3000)
                        await page.click(sel, timeout=3000)
                        await page.wait_for_timeout(2500)
                        clicked_next = True
                        logger.info(f"Instagram: clicked Next (step {step+1})")
                        break
                    except Exception:
                        pass
                if not clicked_next:
                    break

            await page.wait_for_timeout(2000)

            # Подпись
            for sel in [
                "div[contenteditable='true']",
                "textarea",
                "[aria-label='Write a caption...']",
                "[aria-label='Написать подпись…']",
                "[placeholder='Write a caption...']",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await page.keyboard.type(caption[:2200])
                        logger.info("Instagram: caption typed")
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

            # Поделиться / Share — ждём с большим таймаутом
            shared = False
            for sel in [
                "button:has-text('Share')",
                "button:has-text('Поделиться')",
                "div[role='button']:has-text('Share')",
                "div[role='button']:has-text('Поделиться')",
                "button:has-text('Post')",
                "button:has-text('Опубликовать')",
                "[aria-label='Share']",
                "[aria-label='Поделиться']",
            ]:
                try:
                    await page.wait_for_selector(sel, timeout=6000)
                    await page.click(sel, timeout=5000)
                    shared = True
                    logger.info(f"Instagram: clicked Share with: {sel}")
                    break
                except Exception:
                    pass

            if not shared:
                logger.warning("Instagram: could not click Share button")
                return False

            await page.wait_for_timeout(10000)
            logger.info("Instagram: Reel published ✅")
            return True

        except Exception as e:
            logger.error(f"Instagram post error: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
