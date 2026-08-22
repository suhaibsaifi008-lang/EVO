import re
import urllib.parse
import urllib.request


def fetch_weather(city: str = "") -> str | None:
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C,+%t,+feels+like+%f,+humidity+%h,+wind+%w"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            text = resp.read().decode("utf-8", "ignore").strip()
        return text.encode("ascii", "ignore").decode()
    except Exception:
        return None


def clean_city(text: str) -> str:
    m = re.search(r"\b(?:in|for|at)\s+([a-zA-Z\s]+?)\s*[?.!]*$", text.strip())
    return m.group(1).strip() if m else ""
