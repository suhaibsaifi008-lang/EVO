# Fix these three things, in this order. Do not add new architecture, wrappers, or fallback layers — just make these exact changes.

## 1. Browser is invisible
In config (wherever `tools.browser.headless` is defined, likely `config.py` or the active preset's config), change:
    headless: bool = True
to:
    headless: bool = False

Then, separately, confirm Playwright's actual browser binary is downloaded — this is a
one-time setup step distinct from installing the `playwright` Python package:
    uv run playwright install chromium

If it says "already installed," fine. If it downloads something, that was the real problem.

## 2. Apps don't visibly open despite reporting success
Before touching any code: confirm whether the server process (the one actually running
`launch_application` / `open_url`) is running in the same session as the interactive
desktop the user is looking at. Print `sys.platform` at startup and log it. If it's not
"win32", or if the process was started under a different Windows user/session than the
one currently logged in and viewing the screen, that is the entire bug — no amount of
window-launching code will fix a process that isn't allowed to draw on that desktop.
Do not add retry logic or "verify and relaunch" loops until this is ruled out.

## 3. Microphone permission
This is not a backend/Python issue. It's the browser's own site-level permission for
`http://localhost:8000`. Do not add any backend "permission granted" flags, mock
permission state, or bypass logic. The fix is entirely on the browser side: the user
needs to open the site settings (padlock icon) for localhost:8000 and set Microphone
to Allow there. If the app is currently showing its own "permission denied" message even
after the browser-level permission is granted, check that the frontend is actually
re-querying `navigator.permissions.query({name: "microphone"})` or re-attempting
`getUserMedia()` on retry, rather than reading a cached/stale JS variable from the first
failed attempt.
