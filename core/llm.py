"""Model router: provider-agnostic LLM access for EVO.

Any OpenAI-compatible endpoint works as a provider (OpenAI, OpenRouter,
FreeLLMAPI, LM Studio, Ollama's /v1, vLLM, ...). Ollama is additionally
treated as a native fallback provider.

Model roles (env-configurable, all optional):
    JARVIS_MODEL_PRIMARY    normal conversation   (default: JARVIS_OPENAI_MODEL)
    JARVIS_MODEL_FAST       trivial/simple turns  (default: JARVIS_FAST_MODEL or primary)
    JARVIS_MODEL_REASONING  hard analysis         (default: primary)
    JARVIS_MODEL_VISION     screen/image understanding (default: primary)
    JARVIS_MODEL_FALLBACK   tried when others fail (default: OLLAMA model)

The rest of the codebase never talks to a provider directly - it asks for a
ROLE and this module picks provider+model with automatic failover.
"""
import json
import threading
import urllib.error
import urllib.request
from collections import deque
from typing import Iterator

from . import config

_lock = threading.Lock()
_failures: deque = deque(maxlen=25)  # (ts, provider, model, error)


class LLMUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- plumbing


def _completion(base_url: str, api_key: str, payload: dict, timeout: int) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _stream_completion(base_url: str, api_key: str, payload: dict, timeout: int) -> Iterator[str]:
    """Yield content deltas from an OpenAI-compatible SSE stream."""
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0].get("delta", {}).get("content", "")
            except Exception:
                continue
            if delta:
                yield delta


def _record_failure(provider: str, model: str, error: str) -> None:
    with _lock:
        _failures.append((round(__import__("time").time(), 0), provider, model, str(error)[:160]))


def diagnostics() -> dict:
    with _lock:
        return {
            "providers": [
                {"name": p["name"], "base_url": p["base"], "enabled": p["enabled"]}
                for p in _providers()
            ],
            "roles": {r: _role_model(r) for r in ROLES},
            "recent_failures": list(_failures)[-10:],
        }


# ---------------------------------------------------------------- providers


def _providers() -> list[dict]:
    """Ordered provider chain: primary-compatible API first, Ollama second."""
    provs = []
    if config.llm_enabled():
        provs.append({
            "name": "openai-compatible",
            "base": config.OPENAI_BASE_URL,
            "key": config.OPENAI_API_KEY,
            "default_model": config.OPENAI_MODEL,
            "timeout_bias": 0,
            "enabled": True,
        })
    if config.ollama_ready():
        provs.append({
            "name": "ollama",
            "base": config.OLLAMA_URL,
            "key": "",
            "default_model": config.OLLAMA_MODEL,
            "timeout_bias": 30,
            "enabled": True,
        })
    return provs


ROLES = ("primary", "fast", "reasoning", "vision", "fallback")


def _role_model(role: str) -> str:
    import os

    mapping = {
        "primary": os.environ.get("JARVIS_MODEL_PRIMARY", "") or config.OPENAI_MODEL,
        "fast": os.environ.get("JARVIS_MODEL_FAST", "") or os.environ.get("JARVIS_FAST_MODEL", "") or "",
        "reasoning": os.environ.get("JARVIS_MODEL_REASONING", "") or "",
        "vision": os.environ.get("JARVIS_MODEL_VISION", "") or "",
        "fallback": os.environ.get("JARVIS_MODEL_FALLBACK", "") or "",
    }
    model = mapping.get(role, "") or mapping["primary"]
    return model


def _attempts(role: str, model_override: str) -> list[tuple[dict, str]]:
    """Ordered (provider, model) attempts for a role."""
    out: list[tuple[dict, str]] = []
    primary_model = _role_model("primary")
    role_model = model_override or _role_model(role)
    for prov in _providers():
        if role == "fallback":
            # Explicit fallback model on the main provider first...
            fb = _role_model("fallback")
            if fb and not out:
                out.append((prov, fb))
            # ...then each provider's own default.
            out.append((prov, prov["default_model"]))
            continue
        out.append((prov, role_model or prov["default_model"]))
    if not out:
        raise LLMUnavailable("no language core configured (set JARVIS_OPENAI_API_KEY or install Ollama)")
    # de-dup identical (provider, model)
    seen = set()
    unique = []
    for prov, model in out:
        key = (prov["name"], model)
        if key in seen:
            continue
        seen.add(key)
        unique.append((prov, model))
    return unique


def _vision_payload(messages: list[dict], image_b64: str) -> tuple[list[dict], dict]:
    extra = {"max_tokens": 500}
    content = [
        {"type": "text", "text": messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]
    send_messages = messages[:-1] + [{**messages[-1], "content": content}]
    return send_messages, extra


# ---------------------------------------------------------------- public API


def chat(
    messages: list[dict],
    temperature: float = 0.6,
    model: str = "",
    role: str = "primary",
    vision_image: str | None = None,
    timeout: int = 60,
) -> str:
    """One-shot completion routed by role with provider failover."""
    errors: list[str] = []
    for prov, model_name in _attempts(role, model):
        send_messages, extra = (
            _vision_payload(messages, vision_image) if vision_image else (messages, {})
        )
        try:
            data = _completion(
                prov["base"], prov["key"],
                {"model": model_name, "messages": send_messages,
                 "temperature": temperature, **extra},
                timeout=timeout + prov["timeout_bias"],
            )
            text = data["choices"][0]["message"]["content"].strip()
            if text:
                return text
            errors.append(f"{prov['name']}/{model_name}: empty response")
            _record_failure(prov["name"], model_name, "empty response")
        except Exception as exc:
            errors.append(f"{prov['name']}/{model_name}: {exc}")
            _record_failure(prov["name"], model_name, str(exc))
    raise LLMUnavailable("all language cores failed - " + "; ".join(errors or ["nothing configured"]))


def chat_stream(
    messages: list[dict],
    temperature: float = 0.6,
    model: str = "",
    role: str = "primary",
    timeout: int = 75,
) -> Iterator[str]:
    """Stream content deltas; falls back to a one-shot reply if streaming
    is unsupported by the provider. Always yields at least the full text."""
    errors: list[str] = []
    for prov, model_name in _attempts(role, model):
        try:
            got_any = False
            for delta in _stream_completion(
                prov["base"], prov["key"],
                {"model": model_name, "messages": messages, "temperature": temperature, "stream": True},
                timeout=timeout + prov["timeout_bias"],
            ):
                got_any = True
                yield delta
            if got_any:
                return
            errors.append(f"{prov['name']}/{model_name}: empty stream")
        except Exception as exc:
            errors.append(f"{prov['name']}/{model_name}: {exc}")
            _record_failure(prov["name"], model_name, str(exc))
    # Last resort: non-streaming across all providers.
    try:
        yield chat(messages, temperature=temperature, model=model)
        return
    except Exception as exc:
        errors.append(f"fallback: {exc}")
    raise LLMUnavailable("; ".join(errors))


def chat_vision(prompt: str, image_b64: str, temperature: float = 0.4) -> str:
    return chat([{"role": "user", "content": prompt}], temperature=temperature,
                role="vision", vision_image=image_b64, timeout=90)


# Backward-compat shim used by tests/tools that patched _route/_completion.
def _route(messages, temperature, vision_image, timeout, model_override=""):
    try:
        return chat(messages, temperature=temperature, model=model_override,
                    vision_image=vision_image, timeout=timeout), []
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
