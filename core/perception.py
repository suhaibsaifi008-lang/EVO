import base64
import ctypes
import io
import socket
import urllib.request
from typing import Optional


def _active_hwnd() -> int:
    return ctypes.windll.user32.GetForegroundWindow()


def _window_text(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _exe_of_hwnd(hwnd: int) -> str:
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.c_ulong(260)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.replace("\\", "/").split("/")[-1]
        finally:
            k32.CloseHandle(h)
    except Exception:
        pass
    return ""


def active_window() -> str:
    hwnd = _active_hwnd()
    title = _window_text(hwnd) or "Unknown window"
    exe = _exe_of_hwnd(hwnd)
    return f"{exe} | {title}" if exe else title


ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def visible_windows(limit: int = 15) -> list[str]:
    results: list[str] = []

    @ENUMPROC
    def cb(hwnd, _l):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            text = _window_text(hwnd)
            if text and text not in ("Program Manager", "Windows Input Experience"):
                results.append(text)
        return len(results) < limit

    ctypes.windll.user32.EnumWindows(cb, 0)
    return results[:limit]


def network_status() -> str:
    online = False
    try:
        s = socket.create_connection(("1.1.1.1", 53), timeout=2)
        s.close()
        online = True
    except OSError:
        pass
    local_ip = ""
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass
    public_ip = ""
    if online:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
                public_ip = resp.read().decode()[:45]
        except Exception:
            pass
    state = "online" if online else "offline"
    parts = [f"Network: {state}"]
    if local_ip:
        parts.append(f"local {local_ip}")
    if public_ip:
        parts.append(f"public {public_ip}")
    return "; ".join(parts)


def screen_image_b64(max_width: int = 1280) -> str:
    from PIL import ImageGrab

    img = ImageGrab.grab()
    if img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=72)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def describe_screen(question: str = "") -> str:
    from . import config
    from .llm import chat_vision

    if not config.llm_enabled():
        return "My language core is offline, so I cannot interpret the screen right now."
    image_b64 = screen_image_b64()
    prompt = (
        "You are JARVIS with eyes on the user's screen. Describe concisely (under 90 spoken words) "
        f"what is currently on screen.{' Answer this specifically: ' + question if question else ''}"
    )
    return chat_vision(prompt, image_b64)


def ambient_context_line(enabled: bool = True) -> str:
    if not enabled:
        return "unknown"
    try:
        return active_window()
    except Exception:
        return "unknown"
