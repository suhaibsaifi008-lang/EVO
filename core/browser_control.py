"""R2 — Real browser automation via Playwright with a persistent session."""
import threading
import time
from typing import Optional

from . import db

_lock = threading.RLock()
_pw = None
_browser = None
_context = None
_page: Optional[object] = None


def headless() -> bool:
    return db.get_setting("browser_headless", "1") == "1"


class BrowserUnavailable(RuntimeError):
    pass


def _get_page():
    global _pw, _browser, _context, _page
    with _lock:
        if _page is not None:
            try:
                if not _page.is_closed():
                    return _page
            except Exception:
                pass
            _page = None
        try:
            from playwright.sync_api import sync_playwright

            if _pw is None:
                _pw = sync_playwright().start()
            if _browser is None or not _browser.is_connected():
                _browser = _pw.chromium.launch(headless=headless())
            if _context is None:
                _context = _browser.new_context(viewport={"width": 1366, "height": 850})
            _page = _context.new_page()
            return _page
        except Exception as exc:
            raise BrowserUnavailable(f"browser engine unavailable: {exc}") from exc


def close() -> str:
    global _pw, _browser, _context, _page
    with _lock:
        try:
            if _browser:
                _browser.close()
        except Exception:
            pass
        try:
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _pw = _browser = _context = _page = None
    return "Browser closed."


def navigate(url: str, timeout_ms: int = 25000) -> dict:
    page = _get_page()
    if not url.strip():
        raise ValueError("empty url")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    title = page.title()
    body = ""
    try:
        body = page.inner_text("body")[:400].replace("\n", " ")
    except Exception:
        pass
    return {"title": title, "url": page.url, "preview": body}


def _smart_locate(page, desc: str):
    d = desc.strip()
    attempts = []
    if d.startswith(("#", ".", "//", "[", "input", "button", "a.", "a#")):
        attempts.append(page.locator(d))
    attempts += [
        page.get_by_label(d, exact=False),
        page.get_by_placeholder(d, exact=False),
        page.get_by_role("button", name=d),
        page.get_by_role("link", name=d),
        page.get_by_text(d, exact=False),
        page.locator(f"[aria-label*='{d}' i]"),
        page.locator(f"text={d}"),
    ]
    for loc in attempts:
        try:
            count = loc.count()
        except Exception:
            continue
        for i in range(min(count, 5)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    raise LookupError(f"no visible element matching '{desc}'")


def click(desc: str, retries: int = 2) -> dict:
    """Vision-free DOM click with self-recovery: retry after re-wait on failure."""
    page = _get_page()
    last_error = None
    for attempt in range(retries + 1):
        try:
            el = _smart_locate(page, desc)
            label = (el.inner_text() or el.get_attribute("aria-label") or desc)[:60]
            el.click(timeout=8000)
            page.wait_for_timeout(500)
            return {"clicked": label.strip(), "url": page.url}
        except Exception as exc:
            last_error = exc
            page.wait_for_timeout(900)
    raise LookupError(f"click failed for '{desc}': {last_error}")


def fill(desc: str, text: str, submit: bool = False, retries: int = 2) -> dict:
    page = _get_page()
    last_error = None
    for attempt in range(retries + 1):
        try:
            el = _smart_locate(page, desc)
            el.fill(text, timeout=8000)
            value_ok = True
            try:
                value_ok = text[:40] in (el.input_value() or "")
            except Exception:
                pass
            if submit:
                el.press("Enter")
                page.wait_for_timeout(700)
            return {"filled": desc[:60], "verified_value": value_ok, "submitted": submit}
        except Exception as exc:
            last_error = exc
            page.wait_for_timeout(900)
    raise LookupError(f"fill failed for '{desc}': {last_error}")


def read(max_chars: int = 2600) -> dict:
    page = _get_page()
    title = page.title()
    url = page.url
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    text = " ".join(text.split())[:max_chars]
    return {"title": title, "url": url, "text": text}


def screenshot_to(path: str) -> str:
    page = _get_page()
    page.screenshot(path=path, full_page=True)
    return path


def search_web(query: str) -> str:
    result = navigate(f"https://www.bing.com/search?q={query.replace(' ', '+')}")
    page = _get_page()
    results = []
    try:
        for li in page.locator("#b_results > li").all()[:8]:
            t = li.inner_text().replace("\n", " ").strip()
            if t:
                results.append(t[:160])
    except Exception:
        pass
    if not results:
        return f"Search page loaded ({result['title']}) but no structured results parsed."
    return "\n".join(f"{i+1}. {r}" for i, r in enumerate(results))


def verify(kind: str, value: str) -> bool:
    page = _get_page()
    if kind == "url_contains":
        return value.lower() in page.url.lower()
    if kind == "title_contains":
        return value.lower() in page.title().lower()
    if kind == "text_on_page":
        try:
            return value.lower() in " ".join(page.inner_text("body").split()).lower()
        except Exception:
            return False
    if kind == "element_visible":
        try:
            _smart_locate(page, value)
            return True
        except Exception:
            return False
    raise ValueError(f"unknown verification kind '{kind}'")


def status() -> dict:
    with _lock:
        if _page is None:
            return {"open": False}
        try:
            return {"open": True, "url": _page.url, "title": _page.title()}
        except Exception:
            return {"open": False}
