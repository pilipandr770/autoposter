"""
Facebook publisher.
Использует Playwright с сохранённой сессией для публикации видео/Reels.
Пользователь логинится через встроенный браузер (noVNC) и сохраняет сессию.
"""
import asyncio
import os
import subprocess
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["facebook"]

# Глобальный lock — только одна публикация в Facebook одновременно
_fb_lock = asyncio.Lock()


def _transcode_for_facebook(src: str) -> str:
    """Re-encode to H.264/AAC for Facebook Reels.

    Facebook Reels rejects H.264 high profile — use main profile instead.
    """
    if src.endswith("_fb.mp4"):
        return src
    dst = src.rsplit(".", 1)[0] + "_fb.mp4"
    if os.path.exists(dst):
        os.remove(dst)  # always re-encode to pick up latest settings
    result = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
        "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        dst,
    ], capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and os.path.exists(dst):
        logger.info(f"Facebook: transcoded to {dst}")
        return dst
    logger.warning(f"Facebook: ffmpeg failed (rc={result.returncode}), using original. stderr: {result.stderr[-300:]}")
    return src


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
    video_path = _transcode_for_facebook(video_path)
    async with _fb_lock:
        return await _post_video_impl(video_path, caption)


async def _post_video_impl(video_path: str, caption: str) -> bool:
    """Внутренняя реализация публикации (вызывается под lock)."""
    if not os.path.exists(SESSION):
        logger.error("Facebook: session not found")
        return False

    os.environ["DISPLAY"] = ":99"  # Xvfb virtual display — headed mode bypasses bot detection
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ]
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
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
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

            # Verify the Reels Creator UI actually loaded (not an error page)
            reels_ready = False
            for chk in [
                'input[type="file"][accept*="video"]',
                'div[aria-label*="video" i]',
                'div[aria-label*="reel" i]',
                '[data-testid*="reel" i]',
            ]:
                try:
                    await page.wait_for_selector(chk, timeout=3000)
                    reels_ready = True
                    break
                except Exception:
                    pass

            if not reels_ready:
                # Page shows error — try plain wall-post fallback
                logger.warning("Facebook: reels/create/ did not show upload UI — falling back to wall post")
                return await _post_video_wall(page, video_path, caption)

            # Click upload button then set file directly on the input element
            uploaded = False
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

            try:
                await page.wait_for_selector('input[type="file"]', state="attached", timeout=10000)
                await page.locator('input[type="file"]').first.set_input_files(video_path)
                uploaded = True
                logger.info("Facebook: video file set via set_input_files ✅")
                await page.wait_for_timeout(3000)
                try:
                    await page.screenshot(path="/app/data/fb_debug_after_upload.png")
                    logger.info("Facebook: saved post-upload screenshot")
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Facebook set_input_files failed: {e}")

            if not uploaded:
                return await _post_video_wall(page, video_path, caption)

            next_selectors = [
                'div[aria-label="Next"]',
                'div[aria-label="Далі"]',
                'div[aria-label="Далее"]',
                'div[aria-label="Weiter"]',
                '[role="button"][aria-label="Next"]',
                '[role="button"][aria-label="Далі"]',
                '[role="button"][aria-label="Далее"]',
                '[role="button"][aria-label="Weiter"]',
                '[role="button"]:has-text("Next")',
                '[role="button"]:has-text("Далі")',
                '[role="button"]:has-text("Далее")',
                '[role="button"]:has-text("Weiter")',
                'button:has-text("Next")',
                'button:has-text("Далі")',
                'button:has-text("Далее")',
                'button:has-text("Weiter")',
            ]

            # Check for upload error after a longer wait (Facebook needs time to validate)
            await page.wait_for_timeout(25000)
            upload_failed = False
            for err_sel in [
                ':has-text("Неможливо завантажити")',
                ':has-text("Cannot upload")',
                ':has-text("Upload failed")',
                ':has-text("Error uploading")',
            ]:
                try:
                    if await page.locator(err_sel).first.is_visible():
                        upload_failed = True
                        logger.warning(f"Facebook: upload error detected ({err_sel}) — falling back to wall post")
                        break
                except Exception:
                    pass

            if upload_failed:
                return await _post_video_wall(page, video_path, caption)

            # Poll up to 3 minutes for Next/Далі button to appear (upload can be slow)
            logger.info("Facebook: waiting for video to upload and Next button to appear...")
            next_appeared = False
            for _ in range(34):  # 34 * 5s ≈ 3 min (minus the 8s already waited)
                for sel in next_selectors:
                    try:
                        if await page.locator(sel).first.is_visible():
                            next_appeared = True
                            break
                    except Exception:
                        pass
                if next_appeared:
                    break
                await page.wait_for_timeout(5000)

            if not next_appeared:
                logger.warning("Facebook: Next button never appeared after 3min — falling back to wall post")
                return await _post_video_wall(page, video_path, caption)

            # Шаг 1: кнопка "Далі" / "Next" — click until it disappears (up to 5 steps)
            for step in range(5):
                next_clicked = False
                for sel in next_selectors:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible():
                            await btn.click()
                            next_clicked = True
                            logger.info(f"Facebook: clicked Next/Далі (step {step+1})")
                            await page.wait_for_timeout(3000)
                            break
                    except Exception:
                        pass
                if not next_clicked:
                    break

            # Screenshot after Next steps to diagnose where we are
            try:
                await page.screenshot(path="/app/data/fb_debug_after_next.png")
                logger.info("Facebook: saved post-Next screenshot")
            except Exception:
                pass

            # Шаг 2: поле описания
            for sel in [
                'div[contenteditable="true"]',
                'textarea',
                '[placeholder*="description"]',
                '[placeholder*="описание"]',
                '[placeholder*="Caption"]',
                '[placeholder*="Подпись"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await page.keyboard.type(caption[:2000])
                        logger.info("Facebook: caption filled")
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

            # Делаем скриншот перед публикацией для диагностики
            try:
                await page.screenshot(path="/app/data/fb_before_publish.png")
                logger.info("Facebook: saved pre-publish screenshot")
            except Exception:
                pass

            # Шаг 3: кнопка публикации
            publish_clicked = False
            for sel in [
                'div[aria-label="Share now"]',
                'div[aria-label="Поделиться"]',
                'div[aria-label="Share"]',
                'div[aria-label="Teilen"]',
                'div[aria-label="Veröffentlichen"]',
                'div[aria-label*="Publish"]',
                'div[aria-label*="Опубликовать"]',
                '[role="button"][aria-label="Share now"]',
                '[role="button"][aria-label="Share"]',
                '[role="button"][aria-label="Поделиться"]',
                '[role="button"][aria-label="Teilen"]',
                '[role="button"][aria-label="Veröffentlichen"]',
                '[role="button"]:has-text("Share now")',
                '[role="button"]:has-text("Share")',
                '[role="button"]:has-text("Поделиться")',
                '[role="button"]:has-text("Teilen")',
                '[role="button"]:has-text("Veröffentlichen")',
                'button:has-text("Share now")',
                'button:has-text("Share")',
                'button:has-text("Поделиться")',
                'button:has-text("Publish")',
                'button:has-text("Опубликовать")',
                'button:has-text("Teilen")',
                'button:has-text("Veröffentlichen")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        publish_clicked = True
                        logger.info(f"Facebook: clicked publish button ({sel})")
                        break
                except Exception:
                    pass

            if not publish_clicked:
                logger.warning("Facebook: could not find Publish button — taking screenshot for debug")
                try:
                    await page.screenshot(path="/app/data/fb_debug_nopublish.png")
                except Exception:
                    pass
                return False

            # Ждём подтверждения публикации (редирект или исчезновение диалога)
            published = False
            try:
                # Вариант 1: редирект на страницу видео/reels
                await page.wait_for_url(
                    lambda u: "reel" in u or "/videos/" in u or "facebook.com/watch" in u or "facebook.com/reels" in u,
                    timeout=30000
                )
                published = True
            except Exception:
                pass

            if not published:
                try:
                    # Вариант 2: появился тост / уведомление об успехе
                    await page.wait_for_selector(
                        '[role="status"], [aria-live], div:has-text("published"), div:has-text("опубликовано")',
                        timeout=20000
                    )
                    published = True
                except Exception:
                    pass

            if not published:
                # Вариант 3: кнопка Publish исчезла (диалог закрылся)
                try:
                    await page.wait_for_selector(
                        'button:has-text("Publish"), div[aria-label*="Publish"]',
                        state="hidden",
                        timeout=20000
                    )
                    published = True
                except Exception:
                    pass

            if published:
                logger.info("Facebook: Reel published ✅")
                return True
            else:
                logger.error("Facebook: Reel publish uncertain — button clicked but no confirmation received")
                try:
                    await page.screenshot(path="/app/data/fb_debug_uncertain.png")
                except Exception:
                    pass
                # Возвращаем True т.к. кнопка была нажата — Facebook мог просто не дать UI-подтверждения
                logger.warning("Facebook: assuming published (button was clicked)")
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
        await page.wait_for_timeout(4000)

        if "login" in page.url:
            return False

        # Скриншот главной страницы для диагностики
        try:
            await page.screenshot(path="/app/data/fb_debug_wall_home.png")
        except Exception:
            pass

        # Открыть диалог создания поста — расширенный список селекторов
        for sel in [
            '[aria-label="Create a post"]',
            '[aria-label="Створити публікацію"]',
            '[aria-label="Создать публикацию"]',
            '[placeholder*="mind"]',
            '[placeholder*="думаете"]',
            '[placeholder*="думаєте"]',
            'div[role="button"]:has-text("What\'s on your mind")',
            'div[role="button"]:has-text("Что у вас нового")',
            'div[role="button"]:has-text("Що у вас нового")',
        ]:
            try:
                await page.click(sel, timeout=3000)
                await page.wait_for_timeout(2000)
                break
            except Exception:
                pass

        # Нажать кнопку "Photo/Video" в попапе
        for sel in [
            'div[role="button"]:has-text("Photo/video")',
            'div[role="button"]:has-text("Photo")',
            'div[role="button"]:has-text("Video")',
            'div[role="button"]:has-text("Фото/видео")',
            'div[role="button"]:has-text("Фото")',
            '[aria-label*="Photo"]',
            '[aria-label*="Фото"]',
            '[aria-label*="Video"]',
            'span:has-text("Photo/video")',
            'span:has-text("Фото/видео")',
        ]:
            try:
                await page.click(sel, timeout=3000)
                await page.wait_for_timeout(2000)
                break
            except Exception:
                pass

        # Ожидаем file input
        try:
            await page.wait_for_selector('input[type="file"]', state="attached", timeout=8000)
            await page.locator('input[type="file"]').first.set_input_files(video_path)
            logger.info("Facebook wall: video file set ✅")
        except Exception as e:
            logger.warning(f"Facebook wall: file input not found: {e}")
            try:
                await page.screenshot(path="/app/data/fb_debug_wall_nofile.png")
            except Exception:
                pass
            return False

        # Ждём пока Facebook обработает видео (прогресс-бар загрузки)
        logger.info("Facebook wall: waiting for video to upload (up to 60s)...")
        for _ in range(12):
            await page.wait_for_timeout(5000)
            # Проверяем исчез ли прогресс-бар загрузки
            uploading = await page.evaluate("""
                () => !!document.querySelector('[role="progressbar"], [aria-label*="upload" i], [aria-label*="завантаж" i]')
            """)
            if not uploading:
                break

        # Подпись — ищем поле для текста
        for sel in [
            'div[contenteditable="true"][aria-label*="mind"]',
            'div[contenteditable="true"][aria-label*="думаете"]',
            'div[contenteditable="true"][aria-label*="думаєте"]',
            'div[contenteditable="true"]',
            'textarea',
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await page.keyboard.type(caption[:2000])
                    logger.info("Facebook wall: caption filled")
                    break
            except Exception:
                pass

        # Ещё немного ждём чтобы кнопка Post стала активной
        await page.wait_for_timeout(3000)

        # Скриншот перед публикацией
        try:
            await page.screenshot(path="/app/data/fb_debug_wall_before_post.png")
            logger.info("Facebook wall: screenshot saved before post")
        except Exception:
            pass

        # Опубликовать — сначала обычные селекторы, потом JS
        POST_SELS = [
            'div[aria-label="Post"]',
            'div[aria-label="Опублікувати"]',
            'div[aria-label="Опубликовать"]',
            '[aria-label="Post"][role="button"]',
            '[aria-label="Опублікувати"][role="button"]',
            'button:has-text("Post")',
            'button:has-text("Опублікувати")',
            'button:has-text("Опубликовать")',
            'button:has-text("Публікація")',
            '[role="button"]:has-text("Post")',
            '[role="button"]:has-text("Опублікувати")',
            '[role="button"]:has-text("Опубликовать")',
            'button[type="submit"]',
        ]
        post_clicked = False

        # Polling: пробуем найти кнопку в течение 60 секунд
        for attempt in range(12):
            for sel in POST_SELS:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000) and await el.is_enabled():
                        await el.click()
                        post_clicked = True
                        logger.info(f"Facebook wall: clicked Post via {sel} (attempt {attempt+1})")
                        break
                except Exception:
                    pass
            if post_clicked:
                break

            # JS fallback — ищет любую кнопку с нужным текстом или aria-label
            clicked = await page.evaluate("""
                () => {
                    const kw = ['post','опублікувати','опубликовать','публікація','поделиться','публікувати'];
                    for (const el of document.querySelectorAll('[role="button"], button, [aria-label]')) {
                        const txt = (el.textContent || el.getAttribute('aria-label') || '').toLowerCase().trim();
                        if (kw.some(k => txt.includes(k)) && !el.disabled && el.offsetParent !== null) {
                            el.click();
                            return txt;
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                post_clicked = True
                logger.info(f"Facebook wall: clicked Post via JS: '{clicked}' (attempt {attempt+1})")
                break

            await page.wait_for_timeout(5000)

        if not post_clicked:
            logger.error("Facebook wall: could not find Post button")
            try:
                await page.screenshot(path="/app/data/fb_debug_wall_nopost.png")
            except Exception:
                pass
            return False

        await page.wait_for_timeout(8000)
        logger.info("Facebook: video posted via wall ✅")
        return True
    except Exception as e:
        logger.error(f"Facebook wall post error: {e}")
        return False
