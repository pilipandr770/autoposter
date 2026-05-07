import asyncio
import os
import json
import logging
import time
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

        # Playwright storageState format: cookies is a list of dicts.
        # Avoid login_by_sessionid(): it validates through challenge-prone
        # endpoints even when the saved browser session is still usable.
        if isinstance(data.get("cookies"), list):
            ig_cookies = {
                c["name"]: c["value"]
                for c in data["cookies"]
                if "instagram.com" in c.get("domain", "")
            }
            sessionid = ig_cookies.get("sessionid")
            ds_user_id = ig_cookies.get("ds_user_id")
            if not sessionid or not ds_user_id:
                logger.warning("Instagram: no sessionid in browser session — login via browser first")
                return None
            logger.info("Instagram: initializing API client from browser cookies...")
            cl.settings = {
                "cookies": ig_cookies,
                "authorization_data": {
                    "ds_user_id": ds_user_id,
                    "sessionid": sessionid,
                    "should_use_header_over_cookies": True,
                },
                "last_login": time.time(),
            }
            cl.init()
            logger.info("Instagram: API client initialized from browser cookies")
            return cl

        # Instagrapi native format
        cl.load_settings(SESSION)
        cl.get_timeline_feed()
        logger.info("Instagram: session loaded ✅")
        return cl

    except Exception as e:
        logger.warning(f"Instagram: instagrapi session invalid ({e})")
        return None


def _get_client_from_browser_cookies(use_authorization: bool):
    """Build an instagrapi Client from Playwright cookies without validation calls."""
    from instagrapi import Client

    cookies = _read_browser_cookies()
    if not cookies:
        return None

    sessionid = cookies.get("sessionid")
    ds_user_id = cookies.get("ds_user_id")
    if not sessionid or not ds_user_id:
        return None

    cl = Client()
    cl.delay_range = [2, 5]
    cl.settings = {
        "cookies": cookies,
        "last_login": time.time(),
        "mid": cookies.get("mid"),
        "ig_u_rur": cookies.get("rur"),
    }
    if use_authorization:
        cl.settings["authorization_data"] = {
            "ds_user_id": ds_user_id,
            "sessionid": sessionid,
            "should_use_header_over_cookies": True,
        }
    cl.init()
    original_request_log = cl.request_log

    def capture_request_log(response):
        cl.last_response = response
        try:
            cl.last_json = response.json()
        except Exception:
            cl.last_json = {}
        return original_request_log(response)

    cl.request_log = capture_request_log
    mode = "bearer" if use_authorization else "cookie-only"
    logger.info(f"Instagram: API client initialized from browser cookies ({mode})")
    return cl


def _log_api_failure(client, error: Exception):
    response = getattr(client, "last_response", None)
    details = ""
    if response is not None:
        text = getattr(response, "text", "") or ""
        details = f" status={response.status_code} body={text[:1000]}"
    logger.warning(f"Instagram: API upload failed ({error}){details}")


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


def is_native_session() -> bool:
    """Return True when SESSION is an instagrapi settings file, not Playwright storageState."""
    if not os.path.exists(SESSION):
        return False
    try:
        with open(SESSION) as f:
            data = json.load(f)
        return not isinstance(data.get("cookies"), list)
    except Exception:
        return False


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

    # Normalize TikTok videos to an Instagram-friendly MP4. Instagram can silently
    # ignore files with odd frame rates/codecs while keeping the upload dialog open.
    import subprocess
    transcoded = video_path.rsplit(".", 1)[0] + "_ig.mp4"
    if not video_path.endswith("_ig.mp4"):
        logger.info(f"Instagram: transcoding {os.path.basename(video_path)} to Instagram-compatible MP4")
        ret = subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30,format=yuv420p",
                "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
                "-preset", "veryfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                transcoded,
            ],
            capture_output=True,
            text=True,
        )
        if ret.returncode != 0:
            logger.warning(f"Instagram: ffmpeg transcode failed: {ret.stderr[-1000:]}")
            transcoded = video_path
    if os.path.exists(transcoded):
        video_path = transcoded

    # 1) Try native instagrapi session (if logged in via username/password)
    if is_native_session():
        try:
            cl = _get_client()
            if cl is not None:
                logger.info(f"Instagram: uploading Reel via native API ({os.path.basename(video_path)})...")
                media = cl.clip_upload(video_path, caption=caption[:2200])
                logger.info(f"Instagram: Reel published via API ✅ (pk={media.pk})")
                return True
        except Exception as e:
            _log_api_failure(cl, e)

    # 2) Try browser cookies via instagrapi (cookie-only, then bearer)
    for use_authorization in (False, True):
        cl = _get_client_from_browser_cookies(use_authorization)
        if cl is None:
            continue
        mode = "bearer" if use_authorization else "cookie-only"
        try:
            logger.info(f"Instagram: uploading Reel via API/{mode} ({os.path.basename(video_path)})...")
            media = cl.clip_upload(video_path, caption=caption[:2200])
            logger.info(f"Instagram: Reel published via API ✅ (pk={media.pk})")
            return True
        except Exception as e:
            _log_api_failure(cl, e)

    # 3) Fallback: Playwright web upload via www.instagram.com
    logger.warning("Instagram: API upload failed, trying Playwright web upload...")
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

    # Prefer Google Chrome (has H.264 support via its bundled ffmpeg) over
    # Playwright's Chromium (which omits H.264 on Linux due to licensing).
    # Instagram's web upload requires H.264 decode for client-side preview.
    import shutil as _shutil
    _chrome_bin = (
        _shutil.which("google-chrome") or
        _shutil.which("google-chrome-stable") or
        _shutil.which("google-chrome-beta") or
        "/usr/bin/google-chrome"
    )
    _chrome_kwargs = {"executable_path": _chrome_bin} if _shutil.which("google-chrome") or _shutil.which("google-chrome-stable") else {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            **_chrome_kwargs,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,900",
                "--ignore-gpu-blocklist",
                "--disable-gpu-sandbox",
                "--disable-software-rasterizer",
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

        # Anti-fingerprinting: spoof navigator.plugins so Instagram doesn't
        # detect an empty-plugins automation context and block the upload.
        await ctx.add_init_script("""
            (() => {
                // Fake three common Chrome plugins
                const mkPlugin = (name, fn, desc, mimes) => {
                    const p = Object.create(Plugin.prototype);
                    Object.defineProperties(p, {
                        name:        { value: name,  enumerable: true },
                        filename:    { value: fn,    enumerable: true },
                        description: { value: desc,  enumerable: true },
                        length:      { value: mimes.length, enumerable: true },
                    });
                    mimes.forEach((m, i) => { p[i] = m; });
                    return p;
                };
                const pdfMime = Object.create(MimeType.prototype);
                Object.defineProperties(pdfMime, {
                    type:        { value: 'application/pdf' },
                    description: { value: 'Portable Document Format' },
                    suffixes:    { value: 'pdf' },
                });
                const nacl1 = Object.create(MimeType.prototype);
                Object.defineProperties(nacl1, {
                    type: { value: 'application/x-nacl' }, description: { value: '' }, suffixes: { value: '' },
                });
                const nacl2 = Object.create(MimeType.prototype);
                Object.defineProperties(nacl2, {
                    type: { value: 'application/x-pnacl' }, description: { value: '' }, suffixes: { value: '' },
                });

                const fakePlugins = [
                    mkPlugin('Chrome PDF Plugin',  'internal-pdf-viewer',           'Portable Document Format', [pdfMime]),
                    mkPlugin('Chrome PDF Viewer',  'mhjfbmdgcfjbbpaeojofohoefgiehjai', '',                       [pdfMime]),
                    mkPlugin('Native Client',      'internal-nacl-plugin',          '',                         [nacl1, nacl2]),
                ];

                const pluginArray = Object.create(PluginArray.prototype);
                fakePlugins.forEach((p, i) => { pluginArray[i] = p; });
                Object.defineProperties(pluginArray, {
                    length:    { value: fakePlugins.length, enumerable: true },
                    item:      { value: (i) => fakePlugins[i] },
                    namedItem: { value: (n) => fakePlugins.find(x => x.name === n) || null },
                    refresh:   { value: () => {} },
                });

                Object.defineProperty(navigator, 'plugins', {
                    get: () => pluginArray, configurable: true,
                });
                Object.defineProperty(navigator, 'mimeTypes', {
                    get: () => {
                        const arr = Object.create(MimeTypeArray.prototype);
                        const mimes = [pdfMime, nacl1, nacl2];
                        mimes.forEach((m, i) => { arr[i] = m; });
                        Object.defineProperties(arr, {
                            length:    { value: mimes.length, enumerable: true },
                            item:      { value: (i) => mimes[i] },
                            namedItem: { value: (n) => mimes.find(m => m.type === n) || null },
                        });
                        return arr;
                    }, configurable: true,
                });

                // Ensure Chrome object is present (headed Chrome already has it)
                if (!window.chrome) {
                    window.chrome = {
                        app: { isInstalled: false },
                        webstore: { onInstallStageChanged: {}, onDownloadProgress: {} },
                        runtime: { PlatformOs: { MAC:'mac', WIN:'win', ANDROID:'android', CROS:'cros', LINUX:'linux', OPENBSD:'openbsd' }, PlatformArch: { ARM:'arm', X86_32:'x86-32', X86_64:'x86-64' }, RequestUpdateCheckStatus: { THROTTLED:'throttled', NO_UPDATE:'no_update', UPDATE_AVAILABLE:'update_available' }, OnInstalledReason: { INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update' }, OnRestartRequiredReason: { APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic' } },
                    };
                }
            })();
        """)

        page = await ctx.new_page()

        # Capture ALL network requests and console messages from the very start
        _console_msgs = []
        _all_reqs = []     # every request URL for diagnostics
        _upload_reqs = []  # upload-specific
        page.on("console", lambda m: _console_msgs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: _console_msgs.append(f"[pageerror] {e}"))
        page.on("request", lambda r: (
            _all_reqs.append(f"{r.method} {r.url[:120]}"),
            _upload_reqs.append(r.url)
            if any(x in r.url for x in ["upload", "rupload", "media", "create"]) else None,
        ))

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

            # ── Select Reel type from create dialog (if type selector is shown) ──
            # If the create dialog shows different post types (Photo, Reel, Carousel),
            # explicitly click on "Reel" to proceed to the file upload dialog.
            reel_selected = False
            reel_selectors = [
                'div[role="button"]:has-text("Reel")',
                'div[role="button"]:has-text("Reels")',
                'div[role="button"]:has-text("Реель")',
                'button:has-text("Reel")',
                'button:has-text("Reels")',
                'a[href*="/create/reels/"]',
            ]
            for sel in reel_selectors:
                try:
                    await page.click(sel, timeout=2000)
                    reel_selected = True
                    logger.info(f"Instagram web: clicked Reel type with: {sel}")
                    await page.wait_for_timeout(2000)
                    break
                except Exception:
                    pass

            if reel_selected:
                logger.info("Instagram web: Reel type selected, waiting for upload dialog...")
                await page.wait_for_timeout(2000)

            # ── Dismiss any blocking popup dialogs (notifications, cookie banners, etc.) ──
            # Instagram shows a "Turn on Notifications" interstitial after opening the
            # Create dialog. This blocks the upload UI until dismissed.
            # Use JS click to bypass Playwright's click interception and overlay issues.
            async def _dismiss_popups():
                dismissed = await page.evaluate("""
                    () => {
                        const dismissed = [];
                        const dismissTexts = [
                            'not now', 'не сейчас', 'later', 'потом',
                            'skip', 'пропустить', 'dismiss', 'закрыть',
                            'cancel', 'отмена',
                        ];
                        document.querySelectorAll('[role="dialog"]').forEach(dialog => {
                            dialog.querySelectorAll('button,[role="button"]').forEach(btn => {
                                const txt = (btn.textContent || '').trim().toLowerCase();
                                if (dismissTexts.some(t => txt === t || txt.includes(t))) {
                                    btn.click();
                                    dismissed.push(btn.textContent.trim());
                                }
                            });
                        });
                        return dismissed;
                    }
                """)
                if dismissed:
                    logger.info(f"Instagram web: dismissed popups via JS: {dismissed}")
                    await page.wait_for_timeout(800)
                return bool(dismissed)

            await _dismiss_popups()

            # Save debug screenshot (shows create dialog or drag-drop area)
            await page.screenshot(path="/app/data/ig_debug_create.png", full_page=False)
            logger.info("Instagram web: saved create screenshot")

            # ── Upload file ────────────────────────────────────────────────────────
            file_set = False

            # Check automation detection flags + codec support
            webdriver_val = await page.evaluate("() => navigator.webdriver")
            plugins_len = await page.evaluate("() => navigator.plugins.length")
            codec_support = await page.evaluate("""
                () => {
                    const v = document.createElement('video');
                    return {
                        mp4:  v.canPlayType('video/mp4'),
                        h264: v.canPlayType('video/mp4; codecs="avc1.42E01E"'),
                        h264hi: v.canPlayType('video/mp4; codecs="avc1.64001E"'),
                        webm: v.canPlayType('video/webm; codecs="vp8"'),
                        webm9: v.canPlayType('video/webm; codecs="vp9"'),
                    };
                }
            """)
            logger.info(f"Instagram web: navigator.webdriver={webdriver_val}, plugins={plugins_len}, codecs={codec_support}")

            # Inject event spy BEFORE clicking — captures all DOM events with isTrusted
            await page.evaluate("""
                () => {
                    window.__igEvents = [];
                    window.__igConsole = [];
                    ['change','input','dragenter','dragover','drop'].forEach(t => {
                        document.addEventListener(t, (e) => {
                            window.__igEvents.push({
                                type: e.type, isTrusted: e.isTrusted,
                                tag: e.target.tagName,
                                inputType: e.target.type || null,
                                files: e.target.files ? e.target.files.length : null,
                            });
                        }, true);
                    });
                    // Capture console errors
                    const origErr = console.error.bind(console);
                    const origWarn = console.warn.bind(console);
                    console.error = (...a) => { window.__igConsole.push('[err] ' + a.join(' ')); origErr(...a); };
                    console.warn  = (...a) => { window.__igConsole.push('[warn] ' + a.join(' ')); origWarn(...a); };
                }
            """)

            # Selectors for upload-started detection — look for the Next button
            # (appears after file is accepted) or specific upload progress/trim UI.
            # Avoid generic selectors like [role="progressbar"] which are false-positives.
            DIALOG = '[role="dialog"]'
            UPLOAD_READY_SELS = [
                # Next button — only appears AFTER Instagram accepts the file
                'div[role="button"]:has-text("Next")',
                'button:has-text("Next")',
                'div[role="button"]:has-text("Далее")',
                'button:has-text("Далее")',
                # Loading/trim UI that's specific to file accepted
                '[aria-label="Loading"]',
                # Scoped to any dialog
                f'{DIALOG} div[role="button"]:has-text("Next")',
                f'{DIALOG} button:has-text("Next")',
            ]

            async def _upload_started(timeout: int = 800) -> bool:
                for check_sel in UPLOAD_READY_SELS:
                    try:
                        loc = page.locator(check_sel).first
                        if await loc.is_visible(timeout=timeout):
                            txt = await loc.text_content() or ""
                            logger.info(f"Instagram web: upload UI detected ({check_sel!r} text={txt!r})")
                            return True
                    except Exception:
                        pass
                return False

            # Dismiss again — the notifications dialog may appear after Create click
            # with a slight delay that our first dismiss might have missed.
            await _dismiss_popups()
            await page.wait_for_timeout(500)

            # Log what's in the dialog BEFORE clicking select button
            _dialog_content = await page.evaluate("""
                () => {
                    const result = [];
                    document.querySelectorAll('[role="dialog"]').forEach(d => {
                        const btns = [...d.querySelectorAll('button,[role="button"]')].map(b => b.textContent.trim()).filter(Boolean);
                        const h = d.querySelector('h1,h2,h3,[role="heading"]');
                        result.push({title: h ? h.textContent.trim() : '?', btns: btns.slice(0,8)});
                    });
                    return result;
                }
            """)
            logger.info(f"Instagram web: dialog state before select: {_dialog_content}")

            button_sels = [
                'button:has-text("Select from computer")',
                'button:has-text("Выбрать с компьютера")',
                'button:has-text("Выбрать на компьютере")',
                'div[role="button"]:has-text("Select from computer")',
                '[role="button"]:has-text("Select from computer")',
                'button:has-text("Select")',
            ]

            # ── Method A: Real GTK dialog via CDP (disables Playwright interception) ──
            # Playwright always intercepts the file chooser via CDP even in headed mode.
            # Disabling that interception lets the real GTK dialog open so xdotool can
            # type the file path → Chromium fires isTrusted=true change event.
            import subprocess as _sp
            _xdot_env = {**__import__('os').environ, "DISPLAY": ":99"}
            xdotool_ok = _sp.run(['which', 'xdotool'], capture_output=True).returncode == 0

            if xdotool_ok:
                try:
                    # Disable Playwright's file chooser interception
                    cdp = await ctx.new_cdp_session(page)
                    await cdp.send('Page.setInterceptFileChooserDialog', {'enabled': False})
                    logger.info("Instagram web: CDP file chooser interception disabled")

                    # Click "Select from computer" via Playwright CDP
                    btn_clicked = False
                    for sel in button_sels:
                        try:
                            await page.click(sel, timeout=2000)
                            logger.info(f"Instagram web: CDP clicked '{sel}' (native GTK mode)")
                            btn_clicked = True
                            break
                        except Exception:
                            pass
                    if not btn_clicked:
                        found = await page.evaluate('''() => {
                            const texts = ["select from computer","выбрать с компьютера","выбрать на компьютере","select"];
                            for (const el of document.querySelectorAll("button,[role='button']")) {
                                if (texts.some(t => el.textContent.toLowerCase().includes(t))) {
                                    el.click(); return true;
                                }
                            }
                            return false;
                        }''')
                        if found:
                            btn_clicked = True
                            logger.info("Instagram web: CDP clicked via JS fallback (native GTK mode)")

                    if btn_clicked:
                        # Wait for GTK dialog to open
                        await page.wait_for_timeout(2500)

                        # List ALL visible X11 windows to diagnose what opened
                        _wins = _sp.run(['xdotool', 'search', '--onlyvisible', '--name', ''],
                                        env=_xdot_env, capture_output=True, text=True, timeout=3)
                        _win_ids = _wins.stdout.strip().split() if _wins.stdout.strip() else []
                        _win_names = []
                        for wid in _win_ids[:10]:
                            _wn = _sp.run(['xdotool', 'getwindowname', wid],
                                          env=_xdot_env, capture_output=True, text=True, timeout=2)
                            _win_names.append(f"{wid}={_wn.stdout.strip()!r}")
                        logger.info(f"Instagram web: X11 windows after click: {_win_names}")

                        _focused = _sp.run(['xdotool', 'getwindowfocus', '--name'],
                                           env=_xdot_env, capture_output=True, text=True, timeout=3)
                        _focused_id = _sp.run(['xdotool', 'getwindowfocus'],
                                              env=_xdot_env, capture_output=True, text=True, timeout=3)
                        logger.info(f"Instagram web: focused window: id={_focused_id.stdout.strip()} name='{_focused.stdout.strip()}'")

                        # Try to find the GTK dialog by common names
                        _gtk_wid = None
                        for wid in _win_ids:
                            _wn = _sp.run(['xdotool', 'getwindowname', wid],
                                          env=_xdot_env, capture_output=True, text=True, timeout=2)
                            name = _wn.stdout.strip().lower()
                            if any(k in name for k in ['open', 'file', 'select', 'выбрать', 'открыть']):
                                _gtk_wid = wid
                                logger.info(f"Instagram web: GTK dialog found: {wid} = '{_wn.stdout.strip()}'")
                                break

                        if _gtk_wid:
                            # Focus and type into the GTK dialog window
                            _sp.run(['xdotool', 'windowactivate', '--sync', _gtk_wid],
                                    env=_xdot_env, capture_output=True)
                            await page.wait_for_timeout(300)

                        # Type the full path into GTK "Location" bar (Ctrl+L opens it)
                        _sp.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+l'],
                                env=_xdot_env, capture_output=True)
                        await page.wait_for_timeout(400)
                        _sp.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+a'],
                                env=_xdot_env, capture_output=True)
                        _sp.run(['xdotool', 'type', '--clearmodifiers', '--delay', '30',
                                 video_path], env=_xdot_env, capture_output=True)
                        await page.wait_for_timeout(400)
                        _sp.run(['xdotool', 'key', '--clearmodifiers', 'Return'],
                                env=_xdot_env, capture_output=True)
                        await page.wait_for_timeout(2500)

                        # Check captured events to understand what fired
                        ev_log = await page.evaluate("() => window.__igEvents || []")
                        logger.info(f"Instagram web: events after xdotool: {ev_log}")

                        # Check if the file was accepted (GTK dialog closed, file in input)
                        dom_info = await page.evaluate('''() => {
                            const i = document.querySelector('input[type="file"]');
                            return i ? { files: i.files ? i.files.length : 0,
                                         name: i.files && i.files[0] ? i.files[0].name : null } : null;
                        }''')
                        logger.info(f"Instagram web: DOM after xdotool: {dom_info}")
                        if dom_info and dom_info.get('files', 0) > 0:
                            file_set = True
                            logger.info("Instagram web: file set via real GTK dialog ✅")
                            # Immediate screenshot to see dialog state right after file selection
                            await page.screenshot(path="/app/data/ig_debug_after_select.png")
                            logger.info("Instagram web: saved post-select screenshot")

                            # Rapid polling: take a screenshot every second for 15s
                            # and check for upload start or dialog error
                            _req_before = len(_all_reqs)
                            for _sec in range(15):
                                await page.wait_for_timeout(1000)
                                # Dismiss any popups that appeared AFTER file selection
                                await _dismiss_popups()

                                await page.screenshot(
                                    path=f"/app/data/ig_debug_t{_sec+1}s.png",
                                    full_page=False,
                                )
                                _reqs_now = len(_all_reqs)

                                # Check for error message — look in ALL dialogs
                                _err_txt = await page.evaluate("""
                                    () => {
                                        const msgs = [];
                                        document.querySelectorAll('[role="dialog"]').forEach(d => {
                                            const h = d.querySelector('h1,h2,h3,[role="heading"]');
                                            const title = h ? h.textContent.trim() : '';
                                            const btns = [...d.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean);
                                            msgs.push({title, btns});
                                        });
                                        return msgs;
                                    }
                                """)
                                logger.info(
                                    f"Instagram web: t+{_sec+1}s | new_reqs={_reqs_now-_req_before} | "
                                    f"dialogs={_err_txt}"
                                )
                                if await _upload_started(timeout=500):
                                    logger.info(f"Instagram web: upload UI detected at t+{_sec+1}s ✅")
                                    break
                            # Log last 20 network requests since start
                            logger.info(f"Instagram web: total requests since start ({len(_all_reqs)}): "
                                        f"{_all_reqs[-20:]}")

                    # Re-enable interception for cleanup
                    try:
                        await cdp.send('Page.setInterceptFileChooserDialog', {'enabled': True})
                    except Exception:
                        pass

                except Exception as e:
                    logger.warning(f"Instagram web: CDP GTK approach failed: {e}")

            # ── Method A2: Playwright file chooser (fallback if GTK failed) ─────────
            if not file_set:
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
            await page.wait_for_timeout(1500)

            # Quick check: did Method A already trigger the upload?
            if await _upload_started(timeout=2000):
                logger.info("Instagram web: Method A (file chooser) already triggered upload ✅")
            else:
                # ── Method B: React Fiber direct call ──────────────────────────────
                # Call React's onChange prop directly with isTrusted:true so Instagram's
                # handler doesn't reject it as a bot event.
                react_result = await page.evaluate(r"""
                    () => {
                        const input = document.querySelector('input[type="file"]');
                        if (!input) return 'no input';
                        if (!input.files || input.files.length === 0) return 'no files in input';

                        // Build a complete fake event with all properties Instagram might check
                        const fakeEvent = {
                            target: input,
                            currentTarget: input,
                            type: 'change',
                            bubbles: true,
                            cancelable: true,
                            composed: true,
                            isTrusted: true,
                            nativeEvent: {
                                isTrusted: true, type: 'change', target: input,
                                bubbles: true, cancelable: true, composed: true
                            },
                            preventDefault:  () => { fakeEvent.defaultPrevented = true; },
                            stopPropagation: () => { fakeEvent.cancelBubble = true; },
                            stopImmediatePropagation: () => { fakeEvent.cancelBubble = true; },
                            persist:         () => {},
                            defaultPrevented: false,
                            eventPhase: 3,
                            timeStamp: Date.now(),
                        };

                        // Find React's internal property (key name varies by React version)
                        const reactPropsKey = Object.keys(input).find(k =>
                            k.startsWith('__reactProps')
                        );
                        const reactFiberKey = Object.keys(input).find(k =>
                            k.startsWith('__reactFiber') ||
                            k.startsWith('__reactInternalInstance') ||
                            k.startsWith('__reactEventHandlers')
                        );

                        // React 17/18: __reactProps$xxx has the props directly
                        if (reactPropsKey) {
                            const props = input[reactPropsKey];
                            if (typeof props.onChange === 'function') {
                                try {
                                    const fileInfo = input.files[0];
                                    window.__igFileInfo = {
                                        name: fileInfo.name,
                                        size: fileInfo.size,
                                        type: fileInfo.type,
                                        lastModified: fileInfo.lastModified,
                                    };
                                    props.onChange(fakeEvent);
                                } catch(e) { return 'onChange threw: ' + e; }
                                return 'called onChange via __reactProps (isTrusted:true)';
                            }
                        }

                        // React 16 / fallback: walk fiber tree
                        if (reactFiberKey) {
                            let node = input[reactFiberKey];
                            let depth = 0;
                            while (node && depth < 30) {
                                const props = node.memoizedProps || node.pendingProps;
                                if (props && typeof props.onChange === 'function') {
                                    try { props.onChange(fakeEvent); } catch(e) {
                                        return `onChange threw at depth ${depth}: ${e}`;
                                    }
                                    return `called onChange at fiber depth ${depth} (isTrusted:true)`;
                                }
                                node = node.return;
                                depth++;
                            }
                        }
                        return 'onChange not found in any React key';
                    }
                """)
                logger.info(f"Instagram web: React fiber call → {react_result}")
                ev_log2 = await page.evaluate("() => window.__igEvents || []")
                con_log2 = await page.evaluate("() => window.__igConsole || []")
                logger.info(f"Instagram web: events after fiber call: {ev_log2}")
                if con_log2:
                    logger.info(f"Instagram web: console msgs after fiber: {con_log2[:10]}")

                # Wait longer for Instagram's React component to process the file
                # and transition to the editing/cropping UI
                await page.wait_for_timeout(5000)

                # ── Method C: Drag-and-drop via page.route (real File object) ──────
                # Serve the video via Playwright route so we can fetch() it inside the
                # page and get a real File with real binary data — bypassing file-input
                # restrictions while giving Instagram a genuine FileList-like object.
                if not await _upload_started(timeout=1000):
                    import urllib.parse as _ulp
                    _route_url = 'https://ig-local-upload.internal/video.mp4'
                    async def _serve_video(route):
                        await route.fulfill(path=video_path,
                                            content_type='video/mp4')
                    await page.route(_route_url, _serve_video)

                    dnd_result = await page.evaluate(f"""
                        async () => {{
                            const input = document.querySelector('input[type="file"]');
                            // Fetch the file from our internal route
                            let file;
                            try {{
                                const resp = await fetch('{_route_url}');
                                const blob = await resp.blob();
                                file = new File([blob], 'video.mp4', {{type: 'video/mp4'}});
                            }} catch(e) {{
                                return 'fetch failed: ' + e;
                            }}

                            // Build DataTransfer
                            const dt = new DataTransfer();
                            dt.items.add(file);

                            // Find the drop zone
                            const dialog = document.querySelector('[role="dialog"]');
                            // Look for the specific "Drag photos" container
                            const dropZone = (dialog && (
                                [...dialog.querySelectorAll('*')].find(el =>
                                    el.textContent.trim().startsWith('Drag photos') ||
                                    el.textContent.trim().startsWith('Перетащите')
                                )
                            )) || dialog || document.body;

                            // Dispatch drag sequence
                            for (const type of ['dragenter', 'dragover', 'drop']) {{
                                const ev = new DragEvent(type, {{
                                    bubbles: true, cancelable: true, dataTransfer: dt
                                }});
                                dropZone.dispatchEvent(ev);
                            }}

                            // Also try setting file on input if drag didn't work
                            if (input) {{
                                try {{
                                    const ndt = new DataTransfer();
                                    ndt.items.add(file);
                                    Object.defineProperty(input, 'files', {{
                                        value: ndt.files, writable: false, configurable: true
                                    }});
                                }} catch(e) {{}}

                                // Fire real change event after injecting file
                                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                            }}

                            return 'drag+drop dispatched, file size=' + file.size;
                        }}
                    """)
                    logger.info(f"Instagram web: drag-and-drop (real file via route) → {dnd_result}")
                    await page.unroute(_route_url)

            # Wait to see if any of the above triggered the upload UI
            await page.wait_for_timeout(3000)

            # Check if upload already started (Next button visible or progress bar)
            upload_started = await _upload_started()

            if not upload_started:
                logger.info("Instagram web: waiting longer for Instagram to accept selected file...")
                for _ in range(12):
                    await page.wait_for_timeout(5000)
                    upload_started = await _upload_started(timeout=1000)
                    if upload_started:
                        break

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
