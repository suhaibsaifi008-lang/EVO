import urllib.request

from . import config


def push(title: str, message: str) -> bool:
    if not config.NTFY_TOPIC:
        return False
    try:
        safe_title = title.encode("latin-1", "ignore").decode("latin-1")[:120]
        req = urllib.request.Request(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": safe_title, "Tags": "robot", "Priority": "default"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False
