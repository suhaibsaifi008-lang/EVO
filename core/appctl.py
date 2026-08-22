"""R1 completion — app discovery, lifecycle and window management (pure Win32)."""
import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_CLOSE = 0x0010
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5

ENUMPROC = ctypes.WINFUNCTYPE(c_bool := ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _title_of(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _is_visible(hwnd) -> bool:
    return bool(user32.IsWindowVisible(hwnd))


def _enum_visible_windows() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []

    @ENUMPROC
    def cb(hwnd, _l):
        if _is_visible(hwnd):
            t = _title_of(hwnd)
            if t:
                out.append((hwnd, t))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _match_windows(target: str) -> list[tuple[int, str]]:
    needle = target.strip().lower()
    return [(h, t) for h, t in _enum_visible_windows() if needle in t.lower()]


def wait_for_window(target: str, timeout_s: float = 8.0):
    deadline = time.time() + timeout_s
    while True:
        hits = _match_windows(target)
        if hits:
            return hits[0]
        if time.time() >= deadline:
            return None
        time.sleep(0.4)


def list_installed_apps(limit: int = 60) -> list[str]:
    names: set[str] = set()
    dirs = []
    for env in ("PROGRAMDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / r"Microsoft\Windows\Start Menu\Programs")
    for d in dirs:
        if d.exists():
            for p in d.rglob("*.lnk"):
                names.add(p.stem)
    try:
        import winreg

        for hive, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
                           (winreg.KEY_WOW64_32KEY and winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY)):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, flag)
                i = 0
                while True:
                    try:
                        sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        i += 1
                        name, _t = winreg.QueryValueEx(sub, "DisplayName")
                        if name and len(name) > 2 and not name.startswith("{"):
                            names.add(name)
                    except OSError:
                        break
                    except Exception:
                        i += 1
            except OSError:
                continue
    except Exception:
        pass
    return sorted(names)[:limit]


def running_apps(limit: int = 25) -> list[str]:
    titles = [t for _, t in _enum_visible_windows()]
    skip = {"Program Manager", "Windows Input Experience", "Settings", "Microsoft Text Input Application"}
    return [t for t in titles if t not in skip][:limit]


def close_window(target: str, force: bool = False) -> str:
    hits = _match_windows(target)
    if not hits:
        return f"No visible window matching '{target}'."
    closed = 0
    procs = set()
    for hwnd, title in hits:
        if force:
            pid = ctypes.c_uint()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            kernel32_handle = kernel32.OpenProcess(0x0001, False, pid.value)
            if kernel32_handle:
                kernel32.TerminateProcess(kernel32_handle, 1)
                kernel32.CloseHandle(kernel32_handle)
            closed += 1
        else:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed += 1
        procs.add(title[:50])
    if force:
        return f"Force-closed {closed} window(s): {', '.join(list(procs)[:3])}."
    time.sleep(1.2)
    remaining = [t for _, t in _match_windows(target)]
    if remaining:
        return (
            f"Sent close request to '{target}' ({closed} window(s)) but {len(remaining)} remain — "
            f"the app may be asking to save. Say 'force close {target}' to terminate."
        )
    return f"Closed '{target}' — verified gone."


def _show(target: str, command: int, label: str) -> str:
    hits = _match_windows(target)
    if not hits:
        return f"No visible window matching '{target}'."
    count = 0
    for hwnd, _t in hits:
        user32.ShowWindow(hwnd, command)
        count += 1
    placement_ok = True
    return f"{label} {count} window(s) matching '{target}'."


def minimize(target: str) -> str:
    return _show(target, SW_MINIMIZE, "Minimized")


def maximize(target: str) -> str:
    return _show(target, SW_MAXIMIZE, "Maximized")


def restore(target: str) -> str:
    return _show(target, SW_RESTORE, "Restored")


def move_window(target: str, x: int, y: int, w: int = 0, h: int = 0) -> str:
    hits = _match_windows(target)
    if not hits:
        return f"No visible window matching '{target}'."
    hwnd, title = hits[0]
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    flags = 0x0004 | 0x0001
    ok = user32.SetWindowPos(hwnd, 0, int(x), int(y), max(int(w), 200), max(int(h), 150), flags)
    return (f"Moved '{title}' to ({x}, {y})." if w == 0 else
            (f"Moved & resized '{title}' to {w}x{h} at ({x},{y})." if ok else "SetWindowPos failed."))


def focus_window(target: str) -> str:
    hits = _match_windows(target)
    if not hits:
        return f"No visible window matching '{target}'."
    hwnd, title = hits[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.12)
    user32.SetForegroundWindow(hwnd)
    return f"Focused: {title}"
