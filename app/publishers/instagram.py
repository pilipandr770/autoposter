import os
import json
import logging
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from config import SESSION_FILES

logger = logging.getLogger(__name__)
SESSION = SESSION_FILES["instagram"]


def _get_client():
    """Returns an authenticated instagrapi Client.

    Handles both instagrapi JSON format and Playwright storageState format.
    When a Playwright session is detected, extracts sessionid and converts.
    """
    from instagrapi import Client

    if not os.path.exists(SESSION):
        return None

    try:
        with open(SESSION) as f:
            data = json.load(f)

        cl = Client()
        cl.delay_range = [2, 5]

        # Playwright storageState format: cookies is a list of dicts
        if isinstance(data.get("cookies"), list):
            ig_cookies = {
                c["name"]: c["value"]
                for c in data["cookies"]
                if "instagram.com" in c.get("domain", "")
            }
            sessionid = ig_cookies.get("sessionid")
            if not sessionid:
                logger.warning("Instagram: no sessionid in browser session — login via browser first")
                return None
            logger.info("Instagram: converting browser session to instagrapi format...")
            cl.login_by_sessionid(sessionid)
            os.makedirs(os.path.dirname(SESSION), exist_ok=True)
            cl.dump_settings(SESSION)
            logger.info("Instagram: browser session converted ✅")
            return cl

        # Instagrapi native format
        cl.load_settings(SESSION)
        cl.get_timeline_feed()
        logger.info("Instagram: session loaded ✅")
        return cl

    except Exception as e:
        logger.warning(f"Instagram: instagrapi session invalid ({e})")
        return None


def _read_browser_cookies() -> dict | None:
    """Read Instagram cookies from saved Playwright session."""
    if not os.path.exists(SESSION):
        return None
    try:
        with open(SESSION) as f:
            data = json.load(f)
        if isinstance(data.get("cookies"), list):
            return {
                c["name"]: c["value"]
                for c in data["cookies"]
                if "instagram.com" in c.get("domain", "")
            }
        # Instagrapi format — extract cookies from it
        cookies = data.get("cookies", {})
        if isinstance(cookies, dict):
            return cookies
    except Exception:
        pass
    return None


async def login_instagram(username: str, password: str) -> dict:
    """Login via instagrapi and save session to JSON file."""
    from instagrapi import Client
    from instagrapi.exceptions import (
        ChallengeRequired, TwoFactorRequired, BadPassword
    )
    cl = Client()
    cl.delay_range = [2, 5]
    try:
        cl.login(username, password)
        os.makedirs(os.path.dirname(SESSION), exist_ok=True)
        cl.dump_settings(SESSION)
        logger.info("Instagram: logged in and session saved ✅")
        return {"ok": True}
    except TwoFactorRequired:
        os.makedirs(os.path.dirname(SESSION), exist_ok=True)
        cl.dump_settings(SESSION + ".pending")
        return {"ok": False, "error": "2FA_REQUIRED"}
    except BadPassword:
        return {"ok": False, "error": "Неверный логин или пароль"}
    except ChallengeRequired as e:
        return {"ok": False, "error": f"Instagram требует подтверждение: {e}"}
    except Exception as e:
        logger.error(f"Instagram login error: {e}")
        return {"ok": False, "error": str(e)}


async def login_instagram_2fa(username: str, password: str, code: str) -> dict:
    """Complete 2FA login."""
    from instagrapi import Client
    cl = Client()
    cl.delay_range = [2, 5]
    pending = SESSION + ".pending"
    if os.path.exists(pending):
        cl.load_settings(pending)
    try:
        cl.login(username, password, verification_code=code)
        os.makedirs(os.path.dirname(SESSION), exist_ok=True)
        cl.dump_settings(SESSION)
        if os.path.exists(pending):
            os.remove(pending)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def post_reel(video_path: str, caption: str) -> bool:
    """Upload video as Instagram Reel.

    Tries instagrapi first (private API). If the IP is blocked, falls back
    to Playwright web upload using the saved browser session.
    """
    if not os.path.exists(SESSION):
        logger.error("Instagram: session not found — login via browser first")
        return False
    if not os.path.exists(video_path):
        logger.error(f"Instagram: video not found: {video_path}")
        return False

    # Transcode HE-AACv2 → AAC-LC if needed (required by Instagram)
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", video_path],
        capture_output=True, text=True
    )
    needs_transcode = "HE-AAC" in probe.stdout or "he_aac" in probe.stdout.lower()
    if needs_transcode:
        transcoded = video_path.rsplit(".", 1)[0] + "_ig.mp4"
        if not os.path.exists(transcoded):
            logger.info(f"Instagram: transcoding {os.path.basename(video_path)} (HE-AACv2 → AAC-LC)")
            ret = subprocess.run(
                ["ffmpeg", "-y", "-i", video_path,
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", transcoded],
                capture_output=True
            )
            if ret.returncode != 0:
                transcoded = video_path
        video_path = transcoded

    # Try instagrapi first
    try:
        cl = _get_client()
        if cl is not None:
            logger.info(f"Instagram: uploading Reel via API ({os.path.basename(video_path)})...")
            media = cl.clip_upload(video_path, caption=caption[:2200])
            logger.info(f"Instagram: Reel published via API ✅ (pk={media.pk})")
            return True
    except Exception as e:
        logger.warning(f"Instagram: API upload failed ({e}), trying web upload...")

    # Fallback: Playwright web upload via www.instagram.com
    return await _post_reel_via_web(video_path, caption)


async def _post_reel_via_web(video_path: str, caption: str) -> bool:
    """Post Reel via Instagram web UI using Playwright + saved browser session."""
    if not os.path.exists(SESSION):
        return False

    try:
        with open(SESSION) as f:
            session_data = json.load(f)
    except Exception as e:
        logger.error(f"Instagram: cannot read session: {e}")
        return False

    # Build storage_state in Playwright format
    if isinstance(session_data.get("cookies"), list):
        storage_state = session_data  # already Playwright format
    else:
        # Convert instagrapi format to Playwright-compatible storage_state
        raw_cookies = session_data.get("cookies", {})
        if not isinstance(raw_cookies, dict) or "sessionid" not in raw_cookies:
            logger.error("Instagram: no valid session for web upload")
            return False
        cookies = []
        for name, value in raw_cookies.items():
            cookies.append({
                "name": name, "value": value,
                "domain": ".instagram.com", "path": "/",
                "httpOnly": True, "secure": True, "sameSite": "None",
            })
        storage_state = {"cookies": cookies, "origins": []}

    # Run headed (non-headless) inside Xvfb display — Instagram blocks file uploads
    # in headless mode via automation detection. Xvfb is started by BrowserManager at startup.
    import os as _os
    display_env = {**_os.environ, "DISPLAY": ":99"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,900",
            ],
            env=display_env,
        )
        ctx = await browser.new_context(
            storage_state=storage_state,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # Capture console messages and network upload requests for diagnostics
        _console_msgs = []
        _upload_reqs = []
        page.on("console", lambda m: _console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: _console_msgs.append(f"[pageerror] {e}"))
        page.on("request", lambda r: _upload_reqs.append(r.url)
                if any(x in r.url for x in ["upload", "rupload", "media"]) else None)

        try:
            # Navigate to main feed first to ensure session is loaded
            await page.goto("https://www.instagram.com/",
                            wait_until="domcontentloaded", timeout=40000)
            await page.wait_for_timeout(4000)

            if "login" in page.url or "accounts" in page.url:
                logger.error("Instagram: session expired — re-login via browser")
                return False

            logger.info(f"Instagram web: on page {page.url}")

            # Save screenshot of main feed (to verify session + see Create button)
            await page.screenshot(path="/app/data/ig_debug_feed.png", full_page=False)
            logger.info("Instagram web: saved feed screenshot")

            # Click the Create / New post button in the sidebar
            create_clicked = False

            # Selector-based attempts
            for sel in [
                '[aria-label="New post"]',
                '[aria-label="Новая публикация"]',
                '[aria-label="Create"]',
                '[aria-label="Создать"]',
                'svg[aria-label="New post"]',
                'a[href="/create/select/"]',
                'span:has-text("Create")',
                'span:has-text("Новая публикация")',
            ]:
                try:
                    await page.click(sel, timeout=3000)
                    create_clicked = True
                    logger.info(f"Instagram web: clicked Create with: {sel}")
                    break
                except Exception:
                    pass

            # JS fallback — find by aria-label containing key words
            if not create_clicked:
                try:
                    result = await page.evaluate('''() => {
                        const keywords = ["new post", "create", "новая публикация", "создать"];
                        const all = document.querySelectorAll("[aria-label]");
                        for (const el of all) {
                            const label = (el.getAttribute("aria-label") || "").toLowerCase();
                            if (keywords.some(k => label.includes(k))) {
                                const clickable = el.closest("a, button, [role='button'], [role='link']") || el;
                                clickable.click();
                                return el.getAttribute("aria-label");
                            }
                        }
                        return null;
                    }''')
                    if result:
                        create_clicked = True
                        logger.info(f"Instagram web: clicked Create via JS (aria-label='{result}')")
                except Exception as e:
                    logger.warning(f"Instagram web: JS create click failed: {e}")

            if not create_clicked:
                # Fallback: navigate directly to /create/select/
                await page.goto("https://www.instagram.com/create/select/",
                                wait_until="domcontentloaded", timeout=30000)
                logger.info("Instagram web: navigated to /create/select/ directly")

            await page.wait_for_timeout(4000)

            # Save debug screenshot (shows create dialog or drag-drop area)
            await page.screenshot(path="/app/data/ig_debug_create.png", full_page=False)
            logger.info("Instagram web: saved create screenshot")

            # ── Upload file ────────────────────────────────────────────────────────
            # Strategy:
            #   A) Playwright file chooser → get file into browser memory
            #   B) React Fiber direct call → call onChange prop directly (bypasses isTrusted)
            #   C) Drag-and-drop simulation → dispatch drop event with DataTransfer from input files
            #   D) xdotool → type path into native GTK dialog as last resort
            file_set = False
            import subprocess as _sp

            # Selectors scoped to the create dialog (not background feed)
            DIALOG = '[role="dialog"]'
            UPLOAD_READY_SELS = [
                f'{DIALOG} div[role="button"]:has-text("Next")',
                f'{DIALOG} button:has-text("Next")',
                f'{DIALOG} div[role="button"]:has-text("Далее")',
                f'{DIALOG} [aria-label="Loading"]',
                f'{DIALOG} [role="progressbar"]',
            ]

            async def _upload_started(timeout: int = 800) -> bool:
                for check_sel in UPLOAD_READY_SELS:
                    try:
                        if await page.locator(check_sel).first.is_visible(timeout=timeout):
                            logger.info(f"Instagram web: upload UI detected ({check_sel})")
                            return True
                    except Exception:
                        pass
                return False

            button_sels = [
                'button:has-text("Select from computer")',
                'button:has-text("Выбрать с компьютера")',
                'button:has-text("Выбрать на компьютере")',
                'div[role="button"]:has-text("Select from computer")',
                '[role="button"]:has-text("Select from computer")',
                'button:has-text("Select")',
            ]

            # ── Method A: Playwright file chooser ────────────────────────────────
            try:
                async with page.expect_file_chooser(timeout=15000) as fc_info:
                    clicked_sel = False
                    for sel in button_sels:
                        try:
                            await page.click(sel, timeout=2000)
                            logger.info(f"Instagram web: clicked '{sel}' (chooser mode)")
                            clicked_sel = True
                            break
                        except Exception:
                            pass
                    if not clicked_sel:
                        # JS click on button
                        await page.evaluate('''() => {
                            const texts = ["select from computer","выбрать с компьютера","выбрать на компьютере","select"];
                            for (const el of document.querySelectorAll("button,[role='button']")) {
                                if (texts.some(t => el.textContent.toLowerCase().includes(t))) {
                                    el.click(); return true;
                                }
                            }
                            return false;
                        }''')
                fc = await fc_info.value
                await fc.set_files(video_path)
                file_set = True
                logger.info("Instagram web: file set via file chooser ✅")
            except Exception as e:
                logger.warning(f"Instagram web: file chooser failed: {e}")

            # If chooser failed, try set_input_files directly
            if not file_set:
                try:
                    for file_sel in [
                        'input[type="file"][accept*="video"]',
                        'input[type="file"][accept*="mp4"]',
                        'input[type="file"]',
                    ]:
                        inputs = page.locator(file_sel)
                        if await inputs.count() > 0:
                            await inputs.first.set_input_files(video_path)
                            file_set = True
                            logger.info(f"Instagram web: file set via {file_sel} ✅")
                            break
                    if not file_set:
                        logger.warning("Instagram web: no file input found in DOM")
                except Exception as e:
                    logger.warning(f"Instagram web: direct set_input_files failed: {e}")

            if not file_set:
                await page.screenshot(path="/app/data/ig_debug_fail.png", full_page=False)
                logger.error("Instagram web: could not set file — see ig_debug_fail.png")
                return False

            # Short wait for React to register the file in input.files
            await page.wait_for_timeout(1000)

            # ── Method B: React Fiber direct call ────────────────────────────────
            # Directly invoke the React onChange prop on the file input, bypassing
            # the isTrusted event check. Works with React 16/17/18.
            react_result = await page.evaluate(r"""
                () => {
                    const input = document.querySelector('input[type="file"]');
                    if (!input) return 'no input';
                    if (!input.files || input.files.length === 0) return 'no files in input';

                    // Find the React internal property (key name varies by React version)
                    const reactKey = Object.keys(input).find(k =>
                        k.startsWith('__reactFiber') ||
                        k.startsWith('__reactInternalInstance') ||
                        k.startsWith('__reactProps') ||
                        k.startsWith('__reactEventHandlers')
                    );
                    if (!reactKey) return 'no react key on input';

                    // React 17+ stores props directly on __reactProps$xxx
                    if (reactKey.startsWith('__reactProps')) {
                        const props = input[reactKey];
                        if (typeof props.onChange === 'function') {
                            props.onChange({ target: input, currentTarget: input,
                                             type: 'change', bubbles: true });
                            return 'called onChange via __reactProps';
                        }
                    }

                    // Walk the fiber tree upward to find an onChange handler
                    let node = input[reactKey];
                    let depth = 0;
                    while (node && depth < 30) {
                        const props = node.memoizedProps || node.pendingProps;
                        if (props && typeof props.onChange === 'function') {
                            props.onChange({ target: input, currentTarget: input,
                                             type: 'change', bubbles: true });
                            return `called onChange at fiber depth ${depth}`;
                        }
                        node = node.return;
                        depth++;
                    }
                    return 'onChange not found in fiber tree';
                }
            """)
            logger.info(f"Instagram web: React fiber call → {react_result}")

            # ── Method C: Drag-and-drop simulation ───────────────────────────────
            # Build a DataTransfer from the file already in the input, then dispatch
            # a drop event on the dropzone — React's onDrop handler accepts this.
            await page.wait_for_timeout(500)
            dnd_result = await page.evaluate(r"""
                () => {
                    const input = document.querySelector('input[type="file"]');
                    if (!input || !input.files || input.files.length === 0) return 'no file in input';

                    // Build DataTransfer with the file
                    const dt = new DataTransfer();
                    for (const f of input.files) { dt.items.add(f); }

                    // Find the drop target: dialog or the drag-and-drop area
                    const dialog = document.querySelector('[role="dialog"]');
                    const dropZone = (dialog
                        ? dialog.querySelector('[class*="drag"], [class*="drop"], [class*="upload"]')
                        : null) || dialog || document.querySelector('main') || document.body;

                    const dispatch = (type) => {
                        const ev = new DragEvent(type, {
                            bubbles: true, cancelable: true, dataTransfer: dt
                        });
                        dropZone.dispatchEvent(ev);
                    };

                    dispatch('dragenter');
                    dispatch('dragover');
                    dispatch('drop');
                    return `dispatched drag+drop on ${dropZone.tagName}[role=${dropZone.getAttribute('role')}]`;
                }
            """)
            logger.info(f"Instagram web: drag-and-drop simulation → {dnd_result}")

            # Wait to see if any of the above triggered the upload UI
            await page.wait_for_timeout(3000)

            # ── Method D: xdotool (last resort — real GTK file dialog) ───────────
            _xdot_env = {**__import__('os').environ, "DISPLAY": ":99"}
            xdotool_ok = _sp.run(['which', 'xdotool'], capture_output=True).returncode == 0

            # Check if upload already started (Next button visible or progress bar)
            upload_started = await _upload_started()

            if not upload_started and xdotool_ok:
                logger.info("Instagram web: A/B/C did not trigger upload — trying xdotool GTK dialog")
                # Navigate away briefly and back to reset the file input state,
                # then use xdotool to fill the native GTK dialog that Chromium opens
                btn_clicked = False
                for sel in button_sels:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            await btn.scroll_into_view_if_needed()
                            await btn.focus()
                            btn_clicked = True
                            logger.info(f"Instagram web: focused upload button for xdotool ({sel})")
                            break
                    except Exception:
                        pass
                if not btn_clicked:
                    btn_clicked = bool(await page.evaluate('''() => {
                        const texts = ["select from computer","выбрать с компьютера","выбрать на компьютере"];
                        for (const el of document.querySelectorAll("button,[role='button']")) {
                            if (texts.some(t => el.textContent.toLowerCase().includes(t))) {
                                el.focus();
                                return true;
                            }
                        }
                        return false;
                    }''')
                    )

                if btn_clicked:
                    # Use a real X11 key event. Playwright clicks can emit a
                    # filechooser event without opening the native GTK dialog.
                    await page.bring_to_front()
                    await page.wait_for_timeout(500)
                    _sp.run(['xdotool', 'key', '--clearmodifiers', 'Return'],
                            env=_xdot_env, capture_output=True)
                    await page.wait_for_timeout(1500)
                    _focused = _sp.run(['xdotool', 'getwindowfocus', '--name'],
                                       env=_xdot_env, capture_output=True, text=True, timeout=3)
                    logger.info(f"Instagram web: xdotool focused window: '{_focused.stdout.strip()}'")

                    abs_video_path = _os.path.abspath(video_path)
                    _sp.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+l'],
                            env=_xdot_env, capture_output=True)
                    await page.wait_for_timeout(400)
                    _sp.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+a'],
                            env=_xdot_env, capture_output=True)
                    _sp.run(['xdotool', 'type', '--clearmodifiers', '--delay', '20',
                             abs_video_path], env=_xdot_env, capture_output=True)
                    await page.wait_for_timeout(400)
                    _sp.run(['xdotool', 'key', '--clearmodifiers', 'Return'],
                            env=_xdot_env, capture_output=True)
                    await page.wait_for_timeout(5000)
                    logger.info("Instagram web: xdotool file dialog keystrokes sent")
                    upload_started = await _upload_started(timeout=2000)
                    file_set = file_set or upload_started

                if not upload_started:
                    logger.warning("Instagram web: xdotool did not start upload")

            if not upload_started:
                upload_started = await _upload_started(timeout=2000)

            if not upload_started:
                await page.screenshot(path="/app/data/ig_debug_fail.png", full_page=False)
                logger.error("Instagram web: file was selected but upload did not start - see ig_debug_fail.png")
                return False

            # Selectors scoped to the create dialog (not background feed)
            # DIALOG is defined above for upload detection.

            # Wait for Instagram to process the file and show the wizard (crop/edit step)
            # The dialog shows a progress bar while uploading; Next appears only after processing
            logger.info("Instagram web: waiting for upload to complete (up to 90s)...")
            upload_done = False
            for sel in [
                f'{DIALOG} div[role="button"]:has-text("Next")',
                f'{DIALOG} button:has-text("Next")',
                f'{DIALOG} div[role="button"]:has-text("Далее")',
            ]:
                try:
                    await page.wait_for_selector(sel, timeout=90000)
                    upload_done = True
                    logger.info(f"Instagram web: upload complete, Next button appeared ({sel})")
                    break
                except Exception:
                    pass

            if not upload_done:
                await page.screenshot(path="/app/data/ig_debug_fail.png", full_page=False)
                logger.error("Instagram web: Next button never appeared after 90s; upload did not complete")
                logger.info(f"Instagram web: upload-phase console msgs ({len(_console_msgs)} total): {_console_msgs[-20:]}")
                logger.info(f"Instagram web: upload-phase API calls ({len(_upload_reqs)} total): {_upload_reqs[:30]}")
                return False

            await page.screenshot(path="/app/data/ig_debug_after_upload.png", full_page=False)
            logger.info("Instagram web: saved post-upload screenshot")

            # Selectors scoped to the create dialog (not background feed)
            NEXT_SELS = [
                f'{DIALOG} div[role="button"]:has-text("Next")',
                f'{DIALOG} button:has-text("Next")',
                f'{DIALOG} div[role="button"]:has-text("Далее")',
                f'{DIALOG} [aria-label="Next"]',
            ]
            SHARE_SELS = [
                f'{DIALOG} div[role="button"]:has-text("Share")',
                f'{DIALOG} button:has-text("Share")',
                f'{DIALOG} div[role="button"]:has-text("Поделиться")',
                f'{DIALOG} button:has-text("Поделиться")',
                f'{DIALOG} [aria-label="Share"]',
            ]
            CAPTION_SELS = [
                f'{DIALOG} div[aria-label="Write a caption..."]',
                f'{DIALOG} div[aria-label="Напишите подпись..."]',
                f'{DIALOG} textarea[placeholder*="caption" i]',
                f'{DIALOG} div[contenteditable="true"]',
                f'{DIALOG} [aria-label*="caption" i]',
            ]

            # Smart wizard loop: keep clicking Next until Share appears inside dialog
            for step in range(8):
                await page.wait_for_timeout(2500)

                # Check if Share button is visible inside the dialog
                share_found = False
                for sel in SHARE_SELS:
                    try:
                        if await page.locator(sel).first.is_visible(timeout=800):
                            share_found = True
                            break
                    except Exception:
                        pass

                if share_found:
                    logger.info(f"Instagram web: Share button visible after {step} Next click(s)")
                    break

                # Click Next to advance wizard
                next_clicked = False
                for sel in NEXT_SELS:
                    try:
                        btn = page.locator(sel).last
                        if await btn.is_visible(timeout=1500):
                            await btn.click()
                            next_clicked = True
                            logger.info(f"Instagram web: step {step+1} — clicked Next")
                            break
                    except Exception:
                        pass

                if not next_clicked:
                    logger.info(f"Instagram web: no Next at step {step+1}, likely on final step")
                    break

            # Screenshot of final step (caption/share screen)
            await page.screenshot(path="/app/data/ig_debug_share_step.png", full_page=False)

            # Add caption
            caption_added = False
            for sel in CAPTION_SELS:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=2000):
                        await el.click()
                        await el.type(caption[:2200], delay=15)
                        caption_added = True
                        logger.info(f"Instagram web: caption added via {sel}")
                        break
                except Exception:
                    pass

            if not caption_added:
                logger.warning("Instagram web: caption field not found, posting without caption")

            await page.wait_for_timeout(1500)

            # Click Share inside dialog
            shared = False
            for sel in SHARE_SELS:
                try:
                    btn = page.locator(sel).last
                    if await btn.is_visible(timeout=5000):
                        await btn.click()
                        shared = True
                        logger.info(f"Instagram web: clicked Share via {sel}")
                        break
                except Exception:
                    pass

            if not shared:
                await page.screenshot(path="/app/data/ig_debug_share_fail.png", full_page=False)
                logger.error("Instagram web: Share not found — check ig_debug_share_step.png")
                return False

            await page.wait_for_timeout(15000)
            logger.info("Instagram: Reel published via web ✅")
            return True

        except Exception as e:
            logger.error(f"Instagram web post error: {e}", exc_info=True)
            return False
        finally:
            await browser.close()
