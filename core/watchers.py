import hashlib
import threading
import time

from . import db
from .scheduler import dispatcher


def disk_usage_percent(drive: str = "C") -> float:
    from . import pc

    return pc.disk_free_percent(drive)


def _marker(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def evaluate(watcher: dict) -> tuple[bool, str, bool, str]:
    """Returns (fired, human_detail, stays_active, new_last_value)."""
    kind = watcher["kind"]
    target = watcher.get("target") or ""
    threshold = float(watcher.get("threshold") or 0)
    last = watcher.get("last_value") or ""

    if kind == "battery_low":
        from . import pc

        s = pc.system_status()
        pct = s.get("battery_percent")
        if pct is None:
            return False, "no battery detected", True, ""
        if float(pct) <= threshold:
            state = "charging" if s.get("charging") else "discharging"
            return True, f"Battery at {int(pct)}% ({state}) — threshold was {threshold:g}%", False, str(pct)
        return False, f"{int(pct)}%", True, str(pct)

    if kind == "disk_high":
        used = disk_usage_percent(target or "C")
        if used >= threshold:
            return True, f"Drive {target or 'C'} is {used:.0f}% full — threshold {threshold:g}%", False, f"{used}"
        return False, f"{used:.0f}%", True, f"{used}"

    if kind == "website_change":
        from .webtools import fetch_page

        text = fetch_page(target, max_chars=2500)
        marker = _marker(text)
        if last and marker != last:
            excerpt = text.replace("\n", " ")[:180]
            return True, f"The page changed. New content starts: {excerpt}", True, marker
        return False, "unchanged", True, marker or last

    if kind == "news_keyword":
        from .webtools import search_web

        results = search_web(target, max_results=4)
        titles = " | ".join(r["title"] for r in results)
        marker = _marker(titles)
        if last and marker != last:
            return True, f"Fresh results for '{target}': {titles[:220]}", True, marker
        return False, "no change", True, marker or last

    return False, f"unknown watcher kind '{kind}'", False, last


class WatcherEngine(threading.Thread):
    def __init__(self, poll_seconds: int = 30) -> None:
        super().__init__(daemon=True, name="evo-watchers")
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_due()
            except Exception:
                pass
            self._stop.wait(self.poll_seconds)

    def check_due(self) -> int:
        fired = 0
        now = time.time()
        for w in db.due_watchers(now):
            try:
                did_fire, detail, stays_active, new_value = evaluate(w)
            except Exception as exc:
                db.record_watcher(w["id"], w["status"], w["last_value"], now, note=str(exc)[:200])
                continue
            new_status = w["status"] if stays_active else "triggered"
            db.record_watcher(w["id"], new_status, new_value, now, note=detail[:200])
            if did_fire:
                fired += 1
                try:
                    dispatcher.publish({
                        "type": "watcher_alert",
                        "kind": "watcher",
                        "id": w["id"],
                        "text": detail,
                    })
                except Exception:
                    pass
                try:
                    from . import notify

                    notify.push(f"EVO watcher: {w['kind']}", detail[:400])
                except Exception:
                    pass
        return fired


engine = WatcherEngine()
