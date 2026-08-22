import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SHOTS_DIR = DATA_DIR / "screenshots"
DB_PATH = DATA_DIR / "evo.db"

_legacy_db = DATA_DIR / "jarvis.db"
if _legacy_db.exists() and not DB_PATH.exists():
    try:
        _legacy_db.replace(DB_PATH)
    except OSError:
        pass
HOST = os.environ.get("JARVIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("JARVIS_PORT", "8420"))
ACCESS_PIN = os.environ.get("JARVIS_ACCESS_PIN", "").strip()


def _load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

ASSISTANT_NAME = os.environ.get("JARVIS_NAME", "Jarvis")
USER_ADDRESS = os.environ.get("JARVIS_USER_ADDRESS", "sir")
AGENT_MODE = os.environ.get("JARVIS_AGENT_MODE", "1") == "1"
OPENAI_API_KEY = os.environ.get("JARVIS_OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("JARVIS_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("JARVIS_OPENAI_MODEL", "gpt-4o-mini")
FAST_MODEL = os.environ.get("JARVIS_FAST_MODEL", "").strip()
OLLAMA_URL = os.environ.get("JARVIS_OLLAMA_URL", "http://localhost:11434/v1").strip()
OLLAMA_MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "llama3.1").strip()
OLLAMA_ENABLED = os.environ.get("JARVIS_OLLAMA_ENABLED", "1") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("JARVIS_TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = os.environ.get("JARVIS_TELEGRAM_CHAT_IDS", "").strip()
MAIL_ADDRESS = os.environ.get("JARVIS_MAIL_ADDRESS", "").strip()
MAIL_PASSWORD = os.environ.get("JARVIS_MAIL_PASSWORD", "").strip()
SMTP_HOST = os.environ.get("JARVIS_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("JARVIS_SMTP_PORT", "587"))
IMAP_HOST = os.environ.get("JARVIS_IMAP_HOST", "").strip()
IMAP_PORT = int(os.environ.get("JARVIS_IMAP_PORT", "993"))
NTFY_TOPIC = os.environ.get("JARVIS_NTFY_TOPIC", "").strip()
HA_URL = os.environ.get("JARVIS_HA_URL", "").strip().rstrip("/")
HA_TOKEN = os.environ.get("JARVIS_HA_TOKEN", "").strip()


def llm_enabled() -> bool:
    return bool(OPENAI_API_KEY)


def ollama_ready() -> bool:
    return OLLAMA_ENABLED and bool(OLLAMA_URL)


def agent_enabled() -> bool:
    return AGENT_MODE and llm_enabled()


def any_brain_available() -> bool:
    return agent_enabled() or ollama_ready()


for _d in (DATA_DIR, SHOTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
