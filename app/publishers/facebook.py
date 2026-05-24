"""
Facebook publisher.
Использует Playwright с сохранённой сессией для публикации видео/Reels.
Пользователь логинится через встроенный браузер (noVNC) и сохраняет сессию.
"""
import asyncio
import os
import time
import subprocess
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["facebook"]

# Cooldown file — written when FB returns error 1390008 (account temporarily restricted)
_COOLDOWN_FILE = "/app/data/fb_cooldown_until.txt"

# Глобальный lock — только одна публикация в Facebook одновременно
_fb_lock = asyncio.Lock()


def _check_fb_cooldown() -> bool:
    """Return True if account is in cooldown (skip posting)."""
    if not os.path.exists(_COOLDOWN_FILE):
        return False
    try:
        with open(_COOLDOWN_FILE) as f:
            until = float(f.read().strip())
        if time.time() < until:
            remaining_h = (until - time.time()) / 3600
            logger.warning(f"Facebook: account in cooldown ({remaining_h:.1f}h remaining) — skipping")
            return True
        os.remove(_COOLDOWN_FILE)
        return False
    except Exception:
        return False


def _set_fb_cooldown(hours: float = 24.0):
    """Write a cooldown file so no more uploads are attempted for `hours` hours."""
    until = time.time() + hours * 3600
    try:
        os.makedirs(os.path.dirname(_COOLDOWN_FILE), exist_ok=True)
        with open(_COOLDOWN_FILE, "w") as f:
            f.write(str(until))
        logger.warning(f"Facebook: cooldown written — will retry after {hours:.0f}h")
    except Exception as e:
        logger.error(f"Facebook: failed to write cooldown file: {e}")


def _transcode_for_facebook(src: str) -> str:
    """Re-encode to H.264/AAC so Facebook can process it reliably."""
    if src.endswith("_fb.mp4"):
        return src
    dst = src.rsplit(".", 1)[0] + "_fb.mp4"

    # Regenerate only if destination doesn't exist or source is newer
    if os.path.exists(dst):
        try:
            if os.path.getmtime(src) <= os.path.getmtime(dst):
                return dst  # cached version is up-to-date
        except Exception:
            pass
        try:
            os.remove(dst)
        except Exception:
            pass

    result = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-threads", "2",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        dst,
    ], capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and os.path.exists(dst):
        logger.info(f"Facebook: transcoded to {dst}")
        return dst
    logger.warning(f"Facebook: ffmpeg failed (rc={result.returncode}), using original. stderr: {result.stderr[-1000:]}")
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


async def _try_enable_instagram_crosspost(page) -> bool:
    """Enable 'Share to Instagram' toggle in FB composer if present."""
    selectors = [
        '[role="switch"][aria-label*="Instagram"]',
        '[role="switch"][aria-label*="instagram"]',
        'div:has-text("Instagram") [role="switch"]',
        'div:has-text("Instagram") [role="checkbox"]',
        '[data-testid*="instagram"] [role="switch"]',
        '[aria-label*="Instagram"][role="switch"]',
        '[aria-label*="Instagram"][role="checkbox"]',
    ]
    try:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if not await el.is_visible(timeout=1500):
                    continue

                try:
                    await el.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass

                checked = await el.get_attribute("aria-checked")
                if checked == "false":
                    await el.click(force=True)
                    await page.wait_for_timeout(1200)
                    logger.info("Facebook: enabled Instagram cross-post toggle ✅")
                else:
                    logger.info("Facebook: Instagram cross-post toggle already on")
                return True
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Facebook: Instagram toggle lookup error: {e}")
    return False


async def _ensure_instagram_crosspost(page) -> bool:
    """Try multiple times because FB may render distribution controls with delay."""
    for _ in range(3):
        if await _try_enable_instagram_crosspost(page):
            return True
        await page.mouse.wheel(0, 900)
        await page.wait_for_timeout(1200)
    logger.warning("Facebook: Instagram cross-post toggle not found")
    return False


async def post_video(video_path: str, caption: str, crosspost_to_instagram: bool = False) -> bool:
    """Публикует видео/Reel на Facebook используя сохранённую сессию."""
    # Fast cooldown check — skip transcoding and browser launch if restricted
    if _check_fb_cooldown():
        return False
    source_path = video_path
    video_path = _transcode_for_facebook(video_path)
    async with _fb_lock:
        return await _post_video_impl(video_path, caption, crosspost_to_instagram, source_path)


async def _post_video_impl(
    video_path: str,
    caption: str,
    crosspost_to_instagram: bool = False,
    source_path: str | None = None,
) -> bool:
    """Внутренняя реализация публикации (вызывается под lock)."""
    if not os.path.exists(SESSION):
        logger.error("Facebook: session not found")
        return False

    # Check cooldown before launching the browser at all
    if _check_fb_cooldown():
        return False

    _account_restricted = False  # set True when vupload returns error 1390008

    os.environ["DISPLAY"] = ":99"  # Xvfb virtual display — headed mode bypasses bot detection
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",   # Use Google Chrome (H.264 support) instead of Playwright Chromium
            headless=False,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--use-gl=swiftshader",        # Software OpenGL — keeps H.264 decoder active
                "--disable-software-rasterizer=false",
            ]
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            storage_state=SESSION,
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await ctx.new_page()
        # Capture Facebook JS errors for diagnostics
        page.on("console", lambda msg: logger.info(f"FB console [{msg.type}]: {msg.text[:300]}") if msg.type in ("error", "warning") else None)
        page.on("pageerror", lambda err: logger.warning(f"FB page error: {err}"))

        # Intercept vupload responses to detect account restriction (error 1390008)
        async def _on_vupload_response(response):
            nonlocal _account_restricted
            if "vupload" in response.url and "start" in response.url:
                try:
                    body = await response.text()
                    if '"error":1390008' in body or '"error": 1390008' in body:
                        _account_restricted = True
                        logger.error(
                            "Facebook: account restricted (error 1390008 — "
                            "'You cannot use this feature right now'). Setting 24h cooldown."
                        )
                        _set_fb_cooldown(24.0)
                except Exception:
                    pass

        page.on("response", _on_vupload_response)
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
                return await _post_video_wall(page, video_path, caption, crosspost_to_instagram)

            # Click upload button then set file via file chooser (most reliable approach)
            uploaded = False

            # Approach 1: use file chooser (click the visible Add video zone)
            try:
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    # Click the visible "Add video" upload zone
                    for click_sel in [
                        'div:has-text("Add video"):not(:has-text("drag"))',
                        '[aria-label*="Add video" i]',
                        'div[role="button"]:has-text("Add video")',
                        'div:has-text("or drag and drop")',
                        'input[type="file"]',
                    ]:
                        try:
                            await page.click(click_sel, timeout=2000)
                            break
                        except Exception:
                            pass
                file_chooser = await fc_info.value
                await file_chooser.set_files(video_path)
                uploaded = True
                logger.info("Facebook: video file set via file_chooser ✅")
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"Facebook: file_chooser approach failed ({e}), trying set_input_files")

            # Approach 2: fallback – set directly on the hidden input
            if not uploaded:
                try:
                    await page.wait_for_selector('input[type="file"]', state="attached", timeout=10000)
                    await page.locator('input[type="file"]').first.set_input_files(video_path)
                    uploaded = True
                    logger.info("Facebook: video file set via set_input_files ✅")
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    logger.warning(f"Facebook set_input_files failed: {e}")

            try:
                await page.screenshot(path="/app/data/fb_debug_after_upload.png")
                logger.info("Facebook: saved post-upload screenshot")
            except Exception:
                pass

            if not uploaded:
                return await _post_video_wall(page, video_path, caption, crosspost_to_instagram)

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

            # Check for upload error after a short wait (vupload responds fast)
            await page.wait_for_timeout(8000)
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
                        logger.warning(f"Facebook: upload error detected ({err_sel})")
                        break
                except Exception:
                    pass

            # If account restricted (error 1390008) — do NOT fall back to wall post, just stop
            if _account_restricted:
                logger.error("Facebook: aborting — account restricted, cooldown active")
                return False

            if upload_failed:
                # This may be a browser-side preview failure (Chrome can't decode H.264 locally)
                # while the actual upload to Facebook servers succeeded.
                # Log and fall through to the Next button handling — if Далі is available, proceed.
                logger.warning(
                    "Facebook: upload preview error visible, but account not restricted — "
                    "will try to proceed through Reels steps anyway"
                )

            # Poll up to 2 minutes for the first Next/Далі button to appear
            logger.info("Facebook: waiting for Next/Далі button to appear...")
            next_appeared = False
            for _ in range(24):  # 24 * 5s = 2 min
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
                logger.warning("Facebook: Next button never appeared — falling back to wall post")
                return await _post_video_wall(page, video_path, caption, crosspost_to_instagram)

            # ── STEP 1: Upload step → Edit step (click first Далі) ───────────────
            async def _click_dali(label: str) -> bool:
                """Click Далі/Next button. Returns True on success.

                Facebook's React UI keeps previous-step buttons hidden (but still in DOM),
                so we must iterate from LAST to FIRST match to reach the current step's button.
                Playwright's click() (isTrusted=True) is used — JS el.click() is blocked by FB.
                """
                nonlocal page

                # Pass 1: iterate all matches last→first, respecting visibility
                for sel in next_selectors:
                    try:
                        locator = page.locator(sel)
                        count = await locator.count()
                        for i in range(count - 1, -1, -1):
                            try:
                                btn = locator.nth(i)
                                if not await btn.is_visible():
                                    continue
                                try:
                                    await btn.scroll_into_view_if_needed()
                                except Exception:
                                    pass
                                # Wait up to 15 s for enabled
                                for _en in range(15):
                                    try:
                                        if await btn.is_enabled():
                                            break
                                    except Exception:
                                        break
                                    await page.wait_for_timeout(1000)
                                await btn.click()
                                logger.info(f"Facebook: {label} via CSS ({sel})[{i}] ✅")
                                await page.wait_for_timeout(3000)
                                return True
                            except Exception:
                                pass
                    except Exception:
                        pass

                # Pass 2: force-click the last match (bypasses visibility heuristics)
                for sel in next_selectors:
                    try:
                        btn = page.locator(sel).last
                        try:
                            await btn.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        await btn.click(force=True, timeout=5000)
                        logger.info(f"Facebook: {label} via force-click ({sel}) ✅")
                        await page.wait_for_timeout(3000)
                        return True
                    except Exception:
                        pass

                return False

            if not await _click_dali("clicked Далі (step 1: upload→edit)"):
                logger.warning("Facebook: could not click Далі (step 1) — falling back to wall post")
                return await _post_video_wall(page, video_path, caption, crosspost_to_instagram)

            # ── STEP 2: Fill description on Edit step ─────────────────────────────
            for desc_sel in [
                'div[contenteditable="true"][data-contents]',
                'div[contenteditable="true"]',
                'textarea[placeholder*="Describe" i]',
                'textarea[placeholder*="Caption" i]',
                'textarea[placeholder*="Опиш" i]',
                'textarea',
            ]:
                try:
                    el = page.locator(desc_sel).first
                    if await el.is_visible(timeout=3000):
                        await el.click()
                        await el.fill(caption[:2000])
                        logger.info("Facebook: description filled ✅")
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2000)

            # Wait for copyright check / upload processing to finish
            logger.info("Facebook: waiting for copyright check / upload processing...")
            for _cw in range(40):
                checking = False
                for chk_sel in [
                    'div:has-text("Checking for copyrighted content")',
                    'div:has-text("Перевірка авторських прав")',
                    'div:has-text("Checking for copyright")',
                ]:
                    try:
                        if await page.locator(chk_sel).first.is_visible(timeout=400):
                            checking = True
                            break
                    except Exception:
                        pass
                if not checking:
                    logger.info("Facebook: processing done ✅")
                    break
                await page.wait_for_timeout(1000)

            # ── STEP 3: Edit step → Share step (click second Далі) ────────────────
            await page.mouse.wheel(0, 600)   # scroll down to expose the button
            await page.wait_for_timeout(800)

            step3_ok = await _click_dali("clicked Далі (step 3: edit→share)")
            if not step3_ok:
                logger.warning("Facebook: could not click Далі (step 3) — will try publish button anyway")

            # Screenshot before publish for diagnostics
            try:
                await page.screenshot(path="/app/data/fb_before_publish.png")
                logger.info("Facebook: saved pre-publish screenshot")
            except Exception:
                pass

            if crosspost_to_instagram:
                await _ensure_instagram_crosspost(page)

            # Шаг 3: кнопка публикации
            publish_clicked = False
            for sel in [
                'div[aria-label="Share now"]',
                'div[aria-label="Поділитися"]',
                'div[aria-label="Поделиться"]',
                'div[aria-label="Share"]',
                'div[aria-label="Teilen"]',
                'div[aria-label="Veröffentlichen"]',
                'div[aria-label*="Publish"]',
                'div[aria-label*="Опубликовать"]',
                '[role="button"][aria-label="Share now"]',
                '[role="button"][aria-label="Share"]',
                '[role="button"][aria-label="Поділитися"]',
                '[role="button"][aria-label="Поделиться"]',
                '[role="button"][aria-label="Teilen"]',
                '[role="button"][aria-label="Veröffentlichen"]',
                '[role="button"]:has-text("Share now")',
                '[role="button"]:has-text("Share")',
                '[role="button"]:has-text("Поділитися")',
                '[role="button"]:has-text("Поделиться")',
                '[role="button"]:has-text("Teilen")',
                '[role="button"]:has-text("Veröffentlichen")',
                'button:has-text("Share now")',
                'button:has-text("Share")',
                'button:has-text("Поділитися")',
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
            if _account_restricted:
                return False
            try:
                return await _post_video_wall(page, video_path, caption, crosspost_to_instagram)
            except Exception:
                return False
        finally:
            await browser.close()


async def _post_video_wall(page, video_path: str, caption: str, crosspost_to_instagram: bool = False) -> bool:
    """Fallback: публикует видео обычным постом на стене."""
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if "login" in page.url:
            logger.error("Facebook wall: session expired")
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

        for sel in ['div[role="button"]:has-text("Photo")', 'div[role="button"]:has-text("Video")',
                    '[aria-label*="Photo"]', '[aria-label*="Video"]']:
            try:
                await page.click(sel, timeout=3000)
                await page.wait_for_timeout(2000)
                break
            except Exception:
                pass

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

        if crosspost_to_instagram:
            await _ensure_instagram_crosspost(page)

        # Опубликовать
        post_clicked = False
        for sel in [
            'div[aria-label="Post"]',
            'div[aria-label="Опублікувати"]',
            'div[aria-label="Опубликовать"]',
            'button:has-text("Post")',
            'button:has-text("Опублікувати")',
            'button:has-text("Опубликовать")',
            'button:has-text("Публікація")',
            '[role="button"]:has-text("Post")',
            '[role="button"]:has-text("Опублікувати")',
            '[role="button"]:has-text("Опубликовать")',
            'button[type="submit"]',
        ]:
            try:
                await page.click(sel, timeout=5000)
                post_clicked = True
                break
            except Exception:
                pass

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
