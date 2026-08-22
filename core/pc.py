import os
import re
import shutil
import subprocess
import webbrowser
from datetime import datetime
from urllib.parse import quote_plus

from .config import SHOTS_DIR

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
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)


def system_status() -> dict[str, object]:
    ps = (
        "$cpu=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average;"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$total=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "$free=[math]::Round($os.FreePhysicalMemory/1MB,1);"
        "$bat=Get-CimInstance Win32_Battery;"
        "$up=(Get-Date)-$os.LastBootUpTime;"
        "$out=[ordered]@{cpu=$cpu;ramTotalGb=$total;ramFreeGb=$free;"
        "batteryPct=$(if($bat){$bat.EstimatedChargeRemaining}else{$null});"
        "charging=$(if($bat){$bat.BatteryStatus -ge 2}else{$null});"
        "uptimeHours=[math]::Round($up.TotalHours,1)};"
        "$out | ConvertTo-Json -Compress"
    )
    import json

    raw = _run_ps(ps)
    data = json.loads(raw)
    used = round(float(data["ramTotalGb"]) - float(data["ramFreeGb"]), 1)
    return {
        "cpu_percent": data.get("cpu"),
        "ram_used_gb": used,
        "ram_total_gb": data.get("ramTotalGb"),
        "battery_percent": data.get("batteryPct"),
        "charging": bool(data.get("charging")) if data.get("batteryPct") is not None else None,
        "uptime_hours": data.get("uptimeHours"),
    }


def power(action: str) -> None:
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "5"])
    elif action == "restart":
        subprocess.Popen(["shutdown", "/r", "/t", "5"])
    else:
        raise ValueError(f"Unsupported power action: {action}")
