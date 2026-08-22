import ctypes
import os
import re
import shutil
import subprocess
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote_plus

from .config import SHOTS_DIR

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

APP_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "terminal": "wt.exe",
    "task manager": "taskmgr.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "vs code": "code.cmd",
    "vscode": "code.cmd",
    "code": "code.cmd",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
}

URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)


def _run_ps(script: str, timeout: int = 15) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell command failed")
    return (result.stdout or "").strip()


def open_target(query: str) -> str:
    query = query.strip().lower()
    if not query:
        raise ValueError("Nothing to open")
    if URL_RE.match(query):
        url = query if query.startswith("http") else f"https://{query}"
        webbrowser.open(url)
        return url
    if query in APP_ALIASES:
        exe = APP_ALIASES[query]
        resolved = shutil.which(exe) or _resolve_start_menu(exe)
        if resolved:
            os.startfile(resolved)  # noqa: S606
            return query
        raise FileNotFoundError(f"Could not find {exe} on this machine")
    resolved = shutil.which(query) or shutil.which(f"{query}.exe")
    if resolved:
        os.startfile(resolved)  # noqa: S606
        return query
    raise FileNotFoundError(f"I could not find an app called {query}")


def _resolve_start_menu(exe: str) -> str | None:
    dirs = [
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    stem = os.path.splitext(exe)[0].lower()
    for base in dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for name in files:
                if name.lower().startswith(stem) and name.lower().endswith((".lnk", ".exe")):
                    return os.path.join(root, name)
    return None


def web_search(query: str, site: str = "") -> str:
    url = f"https://www.bing.com/search?q={quote_plus(query)}"
    if site == "youtube":
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    elif site == "wikipedia":
        url = f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(query)}"
    webbrowser.open(url)
    return url


def screenshot() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SHOTS_DIR / f"screenshot_{stamp}.png"
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height;"
        "$g = [System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size);"
        f"$bmp.Save('{path.as_posix()}');"
        "$g.Dispose(); $bmp.Dispose()"
    )
    _run_ps(ps)
    os.startfile(SHOTS_DIR)  # noqa: S606
    return str(path)


_MEDIA_KEYS = {
    "play": 0xB3,
    "pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
}
_VOLUME_KEYS = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}


def send_key(vk: int) -> None:
    hexvk = f"0x{vk:X}"
    ps = (
        "Add-Type -Namespace W -Name K -MemberDefinition "
        "'[DllImport(\"user32.dll\")] public static extern void keybd_event(byte k, byte s, uint f, int e);';"
        f"[W.K]::keybd_event({hexvk},0,0,0);[W.K]::keybd_event({hexvk},0,2,0)"
    )
    _run_ps(ps)


def media_key(action: str) -> None:
    key = action.lower()
    if key not in _MEDIA_KEYS:
        raise ValueError(f"Unsupported media action: {action}")
    send_key(_MEDIA_KEYS[key])


def volume(action: str) -> None:
    key = action.lower()
    if key not in _VOLUME_KEYS:
        raise ValueError(f"Unsupported volume action: {action}")
    send_key(_VOLUME_KEYS[key])


def lock_pc() -> None:
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False, creationflags=CREATE_NO_WINDOW)


def system_status() -> dict[str, object]:
    """Pure Win32 API — zero subprocesses, zero window flashes."""
    import ctypes

    cpu = _cpu_percent_native()

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint), ("dwMemoryLoad", ctypes.c_uint),
            ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(mem)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    total_gb = round(mem.ullTotalPhys / (1024 ** 3), 1)
    free_gb = round(mem.ullAvailPhys / (1024 ** 3), 1)
    used_gb = round(total_gb - free_gb, 1)

    bat_pct, charging = _battery_native()

    uptime_hours = None
    try:
        uptime_hours = round(ctypes.windll.kernel32.GetTickCount64() / 3600000.0, 1)
    except Exception:
        pass

    return {
        "cpu_percent": int(cpu) if cpu is not None else None,
        "ram_used_gb": used_gb,
        "ram_total_gb": total_gb,
        "battery_percent": round(bat_pct) if bat_pct is not None else None,
        "charging": charging,
        "uptime_hours": uptime_hours,
    }


class _SPPI(ctypes.Structure):
    _fields_ = [("Idle", ctypes.c_int64), ("Kernel", ctypes.c_int64), ("User", ctypes.c_int64)]


def _cpu_snapshot():
    ntdll = ctypes.WinDLL("ntdll")
    n = os.cpu_count() or 1
    arr = (_SPPI * n)()
    if ntdll.NtQuerySystemInformation(8, arr, ctypes.sizeof(arr), None) != 0:
        raise OSError("NtQuerySystemInformation failed")
    return (
        sum(a.Idle for a in arr),
        sum(a.Kernel for a in arr),
        sum(a.User for a in arr),
    )


def _cpu_percent_native(sample_ms: int = 220):
    try:
        i1, k1, u1 = _cpu_snapshot()
        time.sleep(sample_ms / 1000.0)
        i2, k2, u2 = _cpu_snapshot()
    except Exception:
        return None
    d_idle = max(0, i2 - i1)
    d_kernel = max(0, k2 - k1)
    d_user = max(0, u2 - u1)
    total = d_kernel + d_user
    if total <= 0:
        return 0.0
    busy = max(0, total - d_idle)
    return round(busy / total * 100.0)


def _battery_native():
    try:
        import ctypes

        class BATTERY_STATE(ctypes.Structure):
            _fields_ = [
                ("AcOnLine", ctypes.c_ubyte), ("BatteryPresent", ctypes.c_ubyte),
                ("Charging", ctypes.c_ubyte), ("Discharging", ctypes.c_ubyte),
                ("Spare1", ctypes.c_ubyte * 3), ("Tag", ctypes.c_ubyte),
                ("MaxCapacity", ctypes.c_ulong), ("RemainingCapacity", ctypes.c_ulong),
                ("Rate", ctypes.c_long), ("EstimatedTime", ctypes.c_ulong),
                ("DefaultAlert1", ctypes.c_ulong), ("DefaultAlert2", ctypes.c_ulong),
            ]

        bs = BATTERY_STATE()
        result = ctypes.windll.powrprof.CallNtPowerInformation(
            5, None, 0, ctypes.byref(bs), ctypes.sizeof(bs)
        )
        if result != 0 or not bs.BatteryPresent:
            return None, None
        if bs.MaxCapacity <= 0:
            return None, None
        pct = min(100.0, bs.RemainingCapacity / bs.MaxCapacity * 100.0)
        return pct, bool(bs.AcOnLine)
    except Exception:
        return None, None


def disk_free_percent(drive: str = "C") -> float:
    import ctypes

    drive = "".join(ch for ch in (drive or "C") if ch.isalnum())[:1] or "C"
    free = ctypes.c_uint64()
    total = ctypes.c_uint64()
    ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
        f"{drive}:\\", ctypes.byref(free), ctypes.byref(total), None
    )
    if not ok or total.value == 0:
        raise RuntimeError(f"could not read drive {drive}")
    return round((total.value - free.value) / total.value * 100.0, 1)


def power(action: str) -> None:
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "5"], creationflags=CREATE_NO_WINDOW)
    elif action == "restart":
        subprocess.Popen(["shutdown", "/r", "/t", "5"], creationflags=CREATE_NO_WINDOW)
    else:
        raise ValueError(f"Unsupported power action: {action}")
