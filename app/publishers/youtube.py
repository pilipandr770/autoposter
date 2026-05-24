import os
import subprocess
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

            err = page.locator('[data-error-code], .o6cuMc, [jsname="B34EJ"]')
            if await err.count() > 0:
                return {"ok": False, "error": "Неверный логин или пароль Google"}

            url = page.url
            if "challenge" in url or "signin/v2/challenge" in url:
                return {"ok": False, "error": "2FA_REQUIRED"}

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


def _transcode_for_youtube(src: str) -> str:
    """Re-encode to H.264/AAC so YouTube can process it reliably."""
    if src.endswith("_yt.mp4"):
        return src
    dst = src.rsplit(".", 1)[0] + "_yt.mp4"
    if os.path.exists(dst):
        return dst
    result = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-threads", "2",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        dst,
    ], capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and os.path.exists(dst):
        logger.info(f"YouTube: transcoded to {dst}")
        return dst
    logger.warning(f"YouTube: ffmpeg failed (rc={result.returncode}), using original. stderr: {result.stderr[-300:]}")
    return src


async def _dismiss_identity_dialog(page) -> bool:
    """Dismiss 'Підтвердьте свою особу' and similar dialogs via JS (traverses shadow DOM)."""
    result = await page.evaluate("""
        () => {
            // Remove all backdrops that block pointer events
            document.querySelectorAll(
                'tp-yt-iron-overlay-backdrop, ytcp-dialog-backdrop, [class*="backdrop"]'
            ).forEach(el => el.remove());

            // JS-click the dismiss button — traverses shadow DOM
            const texts = ['Далі', 'Next', 'Continue', 'Далее', 'Got it', 'Понятно', 'OK', 'Ок'];
            const sels  = ['button', '[role="button"]', 'tp-yt-paper-button', 'ytcp-button'];

            function findAndClick(root) {
                for (const sel of sels) {
                    for (const el of root.querySelectorAll(sel)) {
                        const txt = (el.innerText || el.textContent || '').trim();
                        if (texts.some(t => txt === t || txt.startsWith(t))) {
                            el.click();
                            return txt;
                        }
                    }
                }
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) {
                        const r = findAndClick(el.shadowRoot);
                        if (r) return r;
                    }
                }
                return null;
            }
            return findAndClick(document);
        }
    """)
    if result:
        logger.info(f"YouTube: dismissed identity dialog via JS, clicked '{result}'")
        await page.wait_for_timeout(2000)
        return True
    return False


async def post_video(video_path: str, title: str, description: str) -> bool:
    if not os.path.exists(SESSION):
        logger.error("YouTube: session not found")
        return False

    video_path = _transcode_for_youtube(video_path)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            storage_state=SESSION,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        try:
            # Navigate to main Studio first to check session
            await page.goto("https://studio.youtube.com", wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(3000)

            # Try dismissing any account verification / info dialogs that block UI
            await _dismiss_identity_dialog(page)

            if "accounts.google.com" in page.url or "signin" in page.url:
                logger.error("YouTube: session expired or not logged in")
                return False

            # PRIMARY: navigate directly to /upload — this opens the upload dialog without
            # needing to find/click the CREATE button (which changes with each UI update).
            upload_dialog_present = False
            try:
                await page.goto("https://www.youtube.com/upload", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)
                if "accounts.google.com" in page.url or "signin" in page.url:
                    logger.error("YouTube: session expired after /upload redirect")
                    return False
                # Dismiss identity/verification dialogs before waiting for upload dialog
                await _dismiss_identity_dialog(page)
                # Try standard selector first
                try:
                    await page.wait_for_selector(
                        'ytcp-uploads-file-picker, ytcp-uploads-dialog',
                        state="attached", timeout=12000
                    )
                    upload_dialog_present = True
                    logger.info("YouTube: upload dialog opened via /upload URL ✅")
                except Exception:
                    # wait_for_selector can fail even when element is present (shadow DOM).
                    # Fall back to JS check.
                    found = await page.evaluate("""
                        () => !!(document.querySelector('ytcp-uploads-dialog') ||
                                 document.querySelector('ytcp-uploads-file-picker'))
                    """)
                    if found:
                        upload_dialog_present = True
                        logger.info("YouTube: upload dialog confirmed via JS fallback ✅")
            except Exception as e:
                logger.info(f"YouTube: /upload URL approach failed ({e}), trying CREATE button")

            # FALLBACK: try channel-specific upload URL using channel ID from the Studio URL
            if not upload_dialog_present:
                try:
                    await page.goto("https://studio.youtube.com", wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(2000)
                    import re
                    m = re.search(r'/channel/([A-Za-z0-9_-]+)', page.url)
                    if m:
                        cid = m.group(1)
                        await page.goto(
                            f"https://studio.youtube.com/channel/{cid}/videos/upload",
                            wait_until="domcontentloaded", timeout=30000
                        )
                        await page.wait_for_timeout(3000)
                        await _dismiss_identity_dialog(page)
                        await page.wait_for_selector(
                            'ytcp-uploads-file-picker, ytcp-uploads-dialog',
                            state="attached", timeout=10000
                        )
                        upload_dialog_present = True
                        logger.info(f"YouTube: upload dialog via channel URL (channel: {cid}) ✅")
                except Exception as e:
                    logger.info(f"YouTube: channel URL approach failed ({e}), trying CREATE button")

            # Click CREATE button — only needed if upload dialog not already open
            clicked_create = upload_dialog_present  # already open = no need to click
            if not upload_dialog_present:
                create_selectors = [
                    '#create-icon',
                    'ytcp-button#create-icon',
                    'button[aria-label="Create"]',
                    'button[aria-label="Создать"]',
                    'button[aria-label="Створити"]',
                    '[aria-label="Create"]',
                    '[aria-label="Створити"]',
                    'yt-icon-button#create-icon',
                    'ytcp-icon-button#create-icon',
                    'ytcp-create-icon-renderer',
                    '[test-id="create-icon"]',
                ]
                for i, sel in enumerate(create_selectors):
                    try:
                        t = 10000 if i == 0 else 3000  # give more time for first selector
                        await page.wait_for_selector(sel, timeout=t)
                        await page.click(sel, force=True)
                        clicked_create = True
                        logger.info(f"YouTube: clicked CREATE with selector: {sel}")
                        break
                    except Exception:
                        pass

                if not clicked_create:
                    # JS shadow-DOM traversal — CSS selectors don't pierce shadow roots
                    js_result = await page.evaluate("""
                        () => {
                            function findAndClick(root) {
                                // Direct id/selector match
                                const direct = root.querySelector(
                                    '#create-icon, ytcp-create-icon-renderer, [id="create-icon"]'
                                );
                                if (direct) { direct.click(); return 'direct:' + (direct.id || direct.tagName); }
                                // Text-based match on buttons/roles
                                const CREATE_TEXTS = ['Створити', 'Create', 'Создать'];
                                for (const el of root.querySelectorAll('*')) {
                                    const role = el.getAttribute('role');
                                    const tag  = el.tagName.toLowerCase();
                                    if (tag === 'button' || role === 'button' ||
                                        tag.startsWith('ytcp') || tag.startsWith('yt-')) {
                                        const txt = (el.innerText || el.textContent || '').trim();
                                        if (CREATE_TEXTS.some(t => txt === t || txt.endsWith(t))) {
                                            el.click();
                                            return 'js-text:' + txt;
                                        }
                                    }
                                    if (el.shadowRoot) {
                                        const r = findAndClick(el.shadowRoot);
                                        if (r) return r;
                                    }
                                }
                                return null;
                            }
                            return findAndClick(document);
                        }
                    """)
                    if js_result:
                        clicked_create = True
                        logger.info(f"YouTube: clicked CREATE via JS shadow-DOM: {js_result}")
                    else:
                        logger.warning("YouTube: CREATE button not found in shadow DOM either")

            await page.wait_for_timeout(3500)

            # Dismiss again after navigation, in case a dialog appeared
            await _dismiss_identity_dialog(page)

            # Click "Upload video" — only needed if upload dialog not already open
            upload_clicked = upload_dialog_present  # already open = skip
            if not upload_dialog_present:
                upload_texts = [
                    "Завантажити відео",  # Ukrainian YouTube Studio (current)
                    "Додати відео",       # Ukrainian YouTube Studio (alt)
                    "Добавить видео",
                    "Upload video",
                    "Загрузить видео",
                    "Upload",
                ]
                # First: CSS selector approach (force=True bypasses backdrop)
                for text in upload_texts:
                    for sel in [
                        f'tp-yt-paper-item:has-text("{text}")',
                        f'ytcp-menuitem:has-text("{text}")',
                        f'[role="menuitem"]:has-text("{text}")',
                        f'a:has-text("{text}")',
                    ]:
                        try:
                            await page.click(sel, timeout=4000, force=True)
                            upload_clicked = True
                            logger.info(f"YouTube: clicked '{text}' with: {sel}")
                            break
                        except Exception:
                            pass
                    if upload_clicked:
                        break

                if not upload_clicked:
                    # JS shadow-DOM traversal for dropdown item
                    # Clicking the item may trigger page navigation → TargetClosedError is OK
                    for text in upload_texts:
                        try:
                            js_r = await page.evaluate(f"""
                                () => {{
                                    const MENU_SELS = [
                                        'tp-yt-paper-item', 'ytcp-menuitem',
                                        '[role="menuitem"]', '[role="option"]',
                                        'ytcp-ve', 'yt-formatted-string'
                                    ];
                                    function find(root) {{
                                        for (const sel of MENU_SELS) {{
                                            for (const el of root.querySelectorAll(sel)) {{
                                                if ((el.innerText || el.textContent || '').trim().includes('{text}')) {{
                                                    el.click(); return true;
                                                }}
                                            }}
                                        }}
                                        for (const el of root.querySelectorAll('*')) {{
                                            if (el.shadowRoot) {{
                                                if (find(el.shadowRoot)) return true;
                                            }}
                                        }}
                                        return false;
                                    }}
                                    return find(document);
                                }}
                            """)
                        except Exception as nav_err:
                            # Click triggered navigation — page replaced, that's expected
                            logger.info(f"YouTube: '{text}' JS click triggered navigation (expected) — waiting for upload dialog")
                            await page.wait_for_timeout(4000)
                            await _dismiss_identity_dialog(page)
                            try:
                                await page.wait_for_selector(
                                    'ytcp-uploads-file-picker, ytcp-uploads-dialog',
                                    state="attached", timeout=12000
                                )
                                upload_dialog_present = True
                                upload_clicked = True
                                logger.info("YouTube: upload dialog opened after navigation ✅")
                            except Exception:
                                pass
                            break
                        if js_r:
                            upload_clicked = True
                            logger.info(f"YouTube: clicked upload item via JS: '{text}'")
                            break

                if not upload_clicked:
                    logger.error("YouTube: could not click 'Upload video' menu item — saving debug screenshot")
                    await page.screenshot(path="/app/data/yt_debug_menu.png", full_page=False)
                else:
                    logger.info("YouTube: upload dialog should be opening...")

            await page.wait_for_timeout(4000)
            # Screenshot to see if upload dialog opened
            await page.screenshot(path="/app/data/yt_debug_dialog.png", full_page=False)

            # Set file — YouTube Studio embeds input[type="file"] inside shadow DOM of
            # ytcp-uploads-file-picker, so regular wait_for_selector never finds it.
            file_set = False

            # Method 1: JS shadow-DOM traversal (most reliable for YouTube Studio)
            try:
                for _ in range(30):  # poll up to 15s
                    handle = await page.evaluate_handle("""
                        () => {
                            function find(root) {
                                const i = root.querySelector('input[type="file"]');
                                if (i) return i;
                                for (const el of root.querySelectorAll('*'))
                                    if (el.shadowRoot) { const f = find(el.shadowRoot); if (f) return f; }
                                return null;
                            }
                            return find(document);
                        }
                    """)
                    el = handle.as_element()
                    if el:
                        await el.set_input_files(video_path)
                        file_set = True
                        logger.info("YouTube: file set via shadow DOM traversal ✅")
                        break
                    await page.wait_for_timeout(500)
            except Exception as e:
                logger.warning(f"YouTube: shadow DOM traversal failed: {e}")

            if not file_set:
                # Method 2: click "Выбрать файлы" / "Select files" and handle file chooser
                for btn_sel in [
                    'button:has-text("Выбрать файлы")',
                    'button:has-text("Вибрати файли")',
                    'button:has-text("Select files")',
                    'button:has-text("SELECT FILES")',
                    '#select-files-button',
                    'ytcp-uploads-file-picker button',
                    '[class*="select-files"]',
                ]:
                    try:
                        async with page.expect_file_chooser(timeout=8000) as fc_info:
                            await page.click(btn_sel, timeout=4000)
                        fc = await fc_info.value
                        await fc.set_files(video_path)
                        file_set = True
                        logger.info(f"YouTube: file set via file chooser ({btn_sel}) ✅")
                        break
                    except Exception:
                        pass

            if not file_set:
                logger.error("YouTube: all file-set methods failed — aborting")
                return False

            # Wait for editing form to appear
            await page.wait_for_selector('#textbox', timeout=90000)
            await page.wait_for_timeout(3000)

            # Title (force=True bypasses tp-yt-iron-overlay-backdrop)
            title_box = page.locator('#textbox').first
            await title_box.click(click_count=3, force=True)
            await title_box.type(title[:100], delay=30)

            # Description
            try:
                desc_box = page.locator('#textbox').nth(1)
                await desc_box.click(force=True)
                await desc_box.type(description[:4500], delay=10)
            except Exception:
                pass

            await page.wait_for_timeout(1000)

            # Not for kids
            try:
                radio = page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]')
                if await radio.is_visible():
                    await radio.click()
            except Exception:
                pass

            # Next x3
            for _ in range(3):
                for sel in [
                    '#next-button',
                    'ytcp-button#next-button',
                    'button:has-text("Next")',
                    'button:has-text("Далее")',
                    'button:has-text("Далі")',
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3000):
                            await btn.click()
                            await page.wait_for_timeout(2500)
                            break
                    except Exception:
                        pass

            # Public
            try:
                public = page.locator('tp-yt-paper-radio-button[name="PUBLIC"]')
                if await public.is_visible(timeout=5000):
                    await public.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Publish / Done
            for sel in [
                '#done-button',
                'ytcp-button#done-button',
                'button:has-text("Publish")',
                'button:has-text("Опубликовать")',
                'button:has-text("Опублікувати")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=5000):
                        await btn.click()
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(8000)
            logger.info("YouTube: video published ✅")
            return True

        except Exception as e:
            logger.error(f"YouTube post error: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
