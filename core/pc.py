import ctypes
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
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
    "brave": "brave.exe",
    "brave browser": "brave.exe",
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
    "notepad++": "notepad++.exe",
    "vlc": "vlc.exe",
    "zoom": "Zoom.exe",
    "control panel": "control.exe",
    "whatsapp": "WhatsApp.exe",
    "telegram": "Telegram.exe",
    "signal": "Signal.exe",
    "settings": "ms-settings:",
    "pc settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
}

# Web-first destinations: opened in the browser when no native app exists.
SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "whatsapp web": "https://web.whatsapp.com",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://gemini.google.com",
    "copilot": "https://copilot.microsoft.com",
    "microsoft copilot": "https://copilot.microsoft.com",
    "netflix": "https://www.netflix.com",
    "prime video": "https://www.primevideo.com",
    "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com",
    "linkedin": "https://www.linkedin.com",
    "scholarships": "https://scholarships.gov.in",
}

BROWSER_ALIASES = {
    "brave": ["brave", "brave.exe"],
    "brave browser": ["brave", "brave.exe"],
    "chrome": ["chrome", "chrome.exe"],
    "google chrome": ["chrome", "chrome.exe"],
    "edge": ["msedge", "msedge.exe"],
    "microsoft edge": ["msedge", "msedge.exe"],
    "firefox": ["firefox", "firefox.exe"],
}

# Common per-user / per-machine install paths that are NOT on PATH.
_BROWSER_KNOWN_PATHS = {
    "brave": [
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%PROGRAMFILES(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "chrome": [
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"%PROGRAMFILES(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
        r"%PROGRAMFILES(x86)%\Mozilla Firefox\firefox.exe",
    ],
}

FILLER_RE = re.compile(
    r"^(?:the|my|a|an)\s+|\s+(?:app|application|program|please|now|quickly)$"
)

URL_RE = re.compile(r"^(https?://|www\.)\S+$", re.IGNORECASE)
DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+(/[^\s]*)?$", re.IGNORECASE)

_INDEX_LOCK = threading.Lock()
_LNK_INDEX: dict[str, str] = {}
_LNK_TS = 0.0
_UWP_LOCK = threading.Lock()
_UWP_INDEX: dict[str, str] = {}
_UWP_TS = 0.0


def _is_web_address(query: str) -> bool:
    return bool(URL_RE.match(query) or ("." in query and DOMAIN_RE.match(query)))


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


def _launch_uri(target: str) -> str:
    os.startfile(target)  # noqa: S606
    return target


def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            d = Path(base) / r"Microsoft\Windows\Start Menu\Programs"
            if d.exists():
                dirs.append(d)
    return dirs


def _lnk_index() -> dict[str, str]:
    """lowercase shortcut name -> .lnk path. Cached for 10 minutes."""
    global _LNK_INDEX, _LNK_TS
    with _INDEX_LOCK:
        now = time.time()
        if _LNK_INDEX and now - _LNK_TS < 600:
            return _LNK_INDEX
        idx: dict[str, str] = {}
        for base in _start_menu_dirs():
            for p in base.rglob("*.lnk"):
                idx.setdefault(p.stem.strip().lower(), str(p))
        _LNK_INDEX = idx
        _LNK_TS = now
        return idx


def _uwp_index() -> dict[str, str]:
    """All packaged (Microsoft Store) apps: lowercase name -> AUMID. Cached."""
    global _UWP_INDEX, _UWP_TS
    with _UWP_LOCK:
        now = time.time()
        if _UWP_INDEX and now - _UWP_TS < 900:
            return _UWP_INDEX
        apps: dict[str, str] = {}
        try:
            out = _run_ps(
                "Get-StartApps | ForEach-Object { \"$($_.Name)|$($_.AppID)\" }", timeout=25
            )
            for line in out.splitlines():
                parts = line.split("|")
                if len(parts) >= 2:
                    name = "|".join(parts[:-1]).strip().lower()
                    appid = parts[-1].strip()
                    if name and "!" in appid:
                        apps.setdefault(name, appid)
        except Exception:
            pass
        _UWP_INDEX = apps
        _UWP_TS = now
        return apps


def launch_uwp(name: str) -> bool:
    """Launch a packaged/Store app (Copilot, Camera, Xbox...) by fuzzy-exact name."""
    table = _uwp_index()
    key = name.strip().lower()
    appid = table.get(key)
    if not appid:
        candidates = [(n, a) for n, a in table.items() if key in n]
        if len(candidates) == 1:
            appid = candidates[0][1]
    if not appid:
        return False
    try:
        _launch_uri(f"shell:appsFolder\\{appid}")
        return True
    except Exception:
        return False


def default_browser_exe() -> str | None:
    """Path of the user's actual default browser (from the https UserChoice key)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, None)[0]
        m = re.match(r'^"?([^"]+?\.exe)"?', cmd, re.IGNORECASE)
        exe = m.group(1) if m else cmd.split()[0].strip('"')
        return exe if exe and os.path.exists(exe) else None
    except Exception:
        return None


def _resolve_browser(browser: str) -> str | None:
    stem = browser.strip().lower()
    for name in BROWSER_ALIASES.get(stem, [stem]):
        resolved = shutil.which(name) or shutil.which(f"{name}.exe")
        if resolved:
            return resolved
        for raw in _BROWSER_KNOWN_PATHS.get(name, []):
            p = os.path.expandvars(raw)
            if os.path.exists(p):
                return p
        lnk = _resolve_start_menu(name if name.endswith(".exe") else f"{name}.exe")
        if lnk and lnk.lower().endswith(".exe"):
            return lnk
    return None


def open_in_browser(target: str, browser: str = "") -> str:
    """Open a URL, site name, or search phrase in the chosen browser.

    Search phrases are handed to the browser as plain text so it runs them
    through ITS OWN default search engine (Brave Search, Google, ...), never Bing.
    """
    target = target.strip()
    lowered = target.lower().strip(" .!?")
    if _is_web_address(lowered):
        arg = lowered if lowered.startswith("http") else f"https://{lowered}"
    elif lowered in SITES:
        arg = SITES[lowered]
    else:
        arg = target  # non-URL text: Chromium/Firefox search with their own engine
    exe = _resolve_browser(browser) if browser else default_browser_exe()
    if exe:
        subprocess.Popen([exe, "--new-window", arg], creationflags=CREATE_NO_WINDOW)
        return f"{arg} in {_browser_label(exe)}"
    if arg != target:
        # A concrete URL/site: open via OS default browser association.
        webbrowser.open(arg)
        return arg
    # No browser executable resolvable: neutral search fallback (never Bing).
    url = f"https://duckduckgo.com/?q={quote_plus(target)}"
    webbrowser.open(url)
    return url


def _browser_label(exe_path: str) -> str:
    stem = Path(exe_path).stem.lower()
    return {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox"}.get(stem, stem)


def open_target(query: str, browser: str = "") -> str:
    query = query.strip().lower()
    while True:
        stripped = FILLER_RE.sub("", query).strip()
        if stripped == query or not stripped:
            break
        query = stripped
    if not query:
        raise ValueError("Nothing to open")
    if browser:
        return open_in_browser(query, browser)
    if _is_web_address(query):
        url = query if query.startswith("http") else f"https://{query}"
        webbrowser.open(url)
        return url

    # 1. Built-in alias table.
    if query in APP_ALIASES:
        alias = APP_ALIASES[query]
        if alias.endswith(":"):
            _launch_uri(alias)
            return query
        resolved = shutil.which(alias) or shutil.which(f"{alias}.exe") or _resolve_start_menu(alias)
        if resolved:
            os.startfile(resolved)  # noqa: S606
            return query

    # 2. Start Menu shortcut whose name matches exactly (Valorant, games, tools).
    lnk = _lnk_index().get(query)
    if lnk:
        os.startfile(lnk)  # noqa: S606  (Windows resolves .lnk targets itself)
        return query

    # 3. Packaged / Microsoft Store apps (Copilot, Camera, Notion...).
    if launch_uwp(query):
        return query

    # 4. Direct executable lookup.
    resolved = shutil.which(query) or shutil.which(f"{query}.exe")
    if resolved:
        os.startfile(resolved)  # noqa: S606
        return query

    # 5. Single unambiguous fuzzy match in the Start Menu or Store list.
    fuzzy_lnk = [p for name, p in _lnk_index().items() if query in name]
    if len(fuzzy_lnk) == 1:
        os.startfile(fuzzy_lnk[0])  # noqa: S606
        return query

    # 6. Known websites as a fallback destination.
    if query in SITES:
        return open_in_browser(query)

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
                low = name.lower()
                if not low.endswith((".lnk", ".exe")):
                    continue
                file_stem = os.path.splitext(low)[0]
                # Exact name, or a distinct token match ("brave browser" ~ "Brave.lnk" no,
                # "valorant" ~ "Valorant.lnk" yes) so 'code' never matches 'codecov'.
                if file_stem == stem or re.search(rf"(?:^|[\s\-_(]){re.escape(stem)}(?:[\s\-_)]|$)", file_stem):
                    return os.path.join(root, name)
    return None


def web_search(query: str, site: str = "") -> str:
    """Search the web using the user's own browser and its default engine."""
    if site == "youtube":
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    elif site == "wikipedia":
        url = f"https://en.wikipedia.org/wiki/Special:Search?search={quote_plus(query)}"
    else:
        return open_in_browser(query)
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
