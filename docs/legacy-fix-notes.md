# Historical troubleshooting notes (superseded)

These notes describe past issues. For reference, here is how each is
actually handled in the current code:

## 1. Browser visibility
EVO's automation browser (Playwright) is visible by default. The setting
lives in the database (`browser_headless`), not config.py — it defaults to
`0` = visible. One-time setup for Playwright's browser binary:

    .\.venv\Scripts\playwright.exe install chromium

## 2. Apps not opening
App launching runs in the same interactive session as the console. EVO now
resolves apps from: built-in aliases → Start Menu shortcuts (all users) →
Microsoft Store / packaged apps via Get-StartApps → PATH executables →
websites. Window verification is built into the `open_app` tool.

## 3. Microphone permission
The server binds port **8420** (not 8000). Grant microphone permission to
`http://localhost:8420` and to the desktop app window when prompted. If
cloud speech is unavailable, EVO falls back to its offline Vosk engine
automatically; the model (~40MB) downloads once at first server start.
