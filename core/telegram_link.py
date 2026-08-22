import json
import threading
import time
import urllib.parse
import urllib.request

from . import config, db

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()


def telegram_ready() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def _api(method: str, **params) -> dict:
    token = config.TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def allowed_chat_ids() -> list[str]:
    stored = db.get_setting("telegram_chat_id", "")
    env_ids = [c.strip() for c in config.TELEGRAM_CHAT_IDS.split(",") if c.strip()]
    ids = set(env_ids)
    if stored:
        ids.add(stored)
    return sorted(ids)


def _send(chat_id: str, text: str) -> None:
    text = text or "..."
    for i in range(0, len(text), 3900):
        try:
            _api("sendMessage", chat_id=chat_id, text=text[i : i + 3900])
        except Exception:
            pass


def _handle_update(update: dict, handler) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return
    known = allowed_chat_ids()
    if not known:
        db.set_setting("telegram_chat_id", chat_id)
        _send(chat_id, f"Paired. You are now talking to {config.ASSISTANT_NAME}. Only this account is linked.")
        print(f"[telegram] paired new chat {chat_id}", flush=True)
        return
    if chat_id not in known:
        print(f"[telegram] ignored unauthorized chat {chat_id}", flush=True)
        return
    try:
        result = handler(text)
        reply = result.get("reply", "") if isinstance(result, dict) else str(result)
    except Exception as exc:
        reply = f"Something went wrong handling that: {exc}"
    _send(chat_id, reply or "...")


def _loop(handler, offset: int = 0) -> None:
    backoff = 5
    while not _stop.is_set():
        try:
            data = _api("getUpdates", offset=offset, timeout=25, allowed_updates=json.dumps(["message"]))
            backoff = 5
            for update in data.get("result", []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    _handle_update(update, handler)
                except Exception as exc:
                    print(f"[telegram] update error: {exc}", flush=True)
        except Exception as exc:
            print(f"[telegram] poll error: {exc} — retrying in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue


def start(handler) -> bool:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return True
        if not telegram_ready():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(handler,), daemon=True, name="evo-telegram")
        _thread.start()
        print("[telegram] link online", flush=True)
        return True
