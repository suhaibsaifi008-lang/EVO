import json
import urllib.request

from . import config


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if config.HA_TOKEN:
        headers["Authorization"] = f"Bearer {config.HA_TOKEN}"
    return headers


def configured() -> bool:
    return bool(config.HA_URL)


def call_service(domain: str, service: str, entity_id: str = "", data_json: str = "") -> str:
    if not configured():
        return "NOT CONFIGURED: set JARVIS_HA_URL and JARVIS_HA_TOKEN in .env to enable smart-home control."
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if not domain or not service:
        return "ERROR: domain and service are required."
    payload: dict = {}
    if entity_id.strip():
        payload["entity_id"] = entity_id.strip()
    if data_json.strip():
        try:
            extra = json.loads(data_json)
            if isinstance(extra, dict):
                payload.update(extra)
        except Exception:
            return "ERROR: data_json must be valid JSON."
    req = urllib.request.Request(
        f"{config.HA_URL}/api/services/{domain}/{service}",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        target = f" {entity_id}" if entity_id else ""
        return f"Done: {domain}.{service}{target}."
    except Exception as exc:
        return f"Home Assistant error: {exc}"


def get_state(entity_id: str) -> str:
    if not configured():
        return "NOT CONFIGURED: set JARVIS_HA_URL and JARVIS_HA_TOKEN in .env to enable smart-home control."
    entity_id = (entity_id or "").strip()
    req = urllib.request.Request(f"{config.HA_URL}/api/states/{entity_id}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        attrs = ", ".join(f"{k}={v}" for k, v in list((data.get("attributes") or {}).items())[:6])
        return f"{entity_id}: {data.get('state')} ({attrs})"
    except Exception as exc:
        return f"Home Assistant error: {exc}"
