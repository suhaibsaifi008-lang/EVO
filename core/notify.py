import urllib.request

from . import config


def push(title: str, message: str) -> bool:
    if not config.NTFY_TOPIC:
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{config.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title[:120], "Tags": "robot", "Priority": "default"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception:
        return False
