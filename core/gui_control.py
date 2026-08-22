import json
import re
import subprocess
import time
from ctypes import WINFUNCTYPE, c_bool, c_void_p

from . import db


class GUIDisabled(PermissionError):
    pass


def _gate():
    if db.get_setting("gui_allowed", "0") != "1":
        raise GUIDisabled(
            "GUI control is disabled. The user must enable 'Let EVO move the mouse and type' in the Setup tab."
        )
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.08
    return pyautogui


def screen_size() -> dict:
    pg = _gate()
    w, h = pg.size()
    return {"width": int(w), "height": int(h)}


def _clamp(pg, x: float, y: float) -> tuple[int, int]:
    w, h = pg.size()
    return max(0, min(int(x), int(w) - 1)), max(0, min(int(y), int(h) - 1))


def move(x: float, y: float) -> str:
    pg = _gate()
    cx, cy = _clamp(pg, x, y)
    pg.moveTo(cx, cy, duration=0.25)
    return f"Mouse at ({cx}, {cy})."


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> str:
    pg = _gate()
    button = button if button in ("left", "right", "middle") else "left"
    clicks = max(1, min(int(clicks), 3))
    cx, cy = _clamp(pg, x, y)
    pg.click(cx, cy, clicks=clicks, button=button)
    label = {2: "Double-clicked", 3: "Triple-clicked"}.get(clicks, "Clicked")
    return f"{label} ({button}) at ({cx}, {cy})."


def right_click(x: float, y: float) -> str:
    return click(x, y, button="right")


def double_click(x: float, y: float) -> str:
    return click(x, y, clicks=2)


def drag(x1: float, y1: float, x2: float, y2: float, duration: float = 0.6) -> str:
    pg = _gate()
    ax, ay = _clamp(pg, x1, y1)
    bx, by = _clamp(pg, x2, y2)
    pg.moveTo(ax, ay, duration=0.15)
    pg.drag(bx - ax, by - ay, duration=max(0.1, min(float(duration), 3)))
    return f"Dragged from ({ax}, {ay}) to ({bx}, {by})."


def scroll(amount: int) -> str:
    pg = _gate()
    steps = max(-30, min(int(amount), 30))
    pg.scroll(steps * 40)
    return f"Scrolled {'up' if steps > 0 else 'down'} {abs(steps)} notches."


def press_key(key: str) -> str:
    pg = _gate()
    key = key.strip().lower()[:20]
    pg.press(key)
    return f"Pressed {key}."


_HOTKEY_ALIASES = {
    "windows": "win", "win": "win", "cmd": "win", "control": "ctrl", "ctrl": "ctrl",
    "return": "enter", "enter": "enter", "esc": "escape", "escape": "escape",
}


def hotkey(combo: str) -> str:
    pg = _gate()
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts or len(parts) > 4:
        return "ERROR: give a combo like 'ctrl+s' or 'alt+f4'."
    mapped = [_HOTKEY_ALIASES.get(p, p) for p in parts]
    try:
        pg.hotkey(*mapped)
    except Exception as exc:
        return f"ERROR: could not press {'+'.join(mapped)}: {exc}"
    return f"Pressed {'+'.join(mapped)}."


def type_text(text: str) -> str:
    pg = _gate()
    text = (text or "")[:4000]
    if not text:
        return "Nothing to type."
    if all(ord(ch) < 128 for ch in text):
        pg.typewrite(text, interval=0.012)
        return f"Typed {len(text)} characters."
    import base64

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    ps = f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))"
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=10,
    )
    time.sleep(0.15)
    pg.hotkey("ctrl", "v")
    return f"Pasted {len(text)} characters via clipboard."


ENUMPROC = WINFUNCTYPE(c_bool, c_void_p, c_void_p)


def focus_window(title_substring: str) -> str:
    _gate()
    needle = (title_substring or "").strip().lower()
    if not needle:
        return "ERROR: give part of a window title."
    import ctypes

    user32 = ctypes.windll.user32
    matches: list[tuple[int, str]] = []

    @ENUMPROC
    def cb(hwnd, _l):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if title and needle in title.lower():
                matches.append((hwnd, title))
                return False
        return True

    user32.EnumWindows(cb, 0)
    if not matches:
        return f"No visible window matching '{title_substring}'."
    hwnd, title = matches[0]
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.1)
    user32.SetForegroundWindow(hwnd)
    return f"Focused window: {title}"


def click_element(description: str) -> str:
    """Vision-guided click: find UI element by description, then click it."""
    pg = _gate()
    from .perception import screen_image_b64

    image_b64 = screen_image_b64(max_width=1000)
    prompt = (
        f"Locate '{description.strip()[:200]}' on this screenshot of the user's screen. "
        'Reply ONLY with JSON: {"x": <center-x 0-1000>, "y": <center-y 0-1000>} '
        "normalized so the full image spans 0-1000 on both axes."
    )
    from .llm import chat_vision

    raw = chat_vision(prompt, image_b64, temperature=0.0)
    match = re.search(r"\{[^{}]*\"x\"[^{}]*\}", raw, re.DOTALL)
    if not match:
        return f"I could not locate '{description}' on screen. Vision said: {raw[:150]}"
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return f"Vision returned unreadable coordinates for '{description}': {raw[:150]}"
    nx = float(data.get("x", -1))
    ny = float(data.get("y", -1))
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return f"Vision returned out-of-range coordinates for '{description}'."
    w, h = pg.size()
    real_x, real_y = nx / 1000.0 * w, ny / 1000.0 * h
    pg.moveTo(real_x, real_y, duration=0.3)
    time.sleep(0.15)
    pg.click()
    return f"Clicked '{description}' at ({int(real_x)}, {int(real_y)})."
