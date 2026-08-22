import json
import urllib.error
import urllib.request

from . import config


def _completion(base_url: str, api_key: str, payload: dict, timeout: int = 60) -> dict:
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


def _route(messages: list[dict], temperature: float, vision_image: str | None, timeout: int, model_override: str = "") -> tuple[str, list[str]]:
    payload_extra: dict = {}
    content: object
    if vision_image:
        content = [
            {"type": "text", "text": messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{vision_image}"}},
        ]
        payload_extra["max_tokens"] = 300
        send_messages = messages[:-1] + [{**messages[-1], "content": content}]
    else:
        send_messages = messages

    errors: list[str] = []
    primary_model = model_override or config.OPENAI_MODEL
    if config.llm_enabled():
        try:
            data = _completion(
                config.OPENAI_BASE_URL,
                config.OPENAI_API_KEY,
                {"model": primary_model, "messages": send_messages, "temperature": temperature, **payload_extra},
                timeout=timeout,
            )
            return data["choices"][0]["message"]["content"].strip(), errors
        except Exception as exc:
            errors.append(f"primary: {exc}")
    if config.ollama_ready():
        try:
            data = _completion(
                config.OLLAMA_URL,
                "",
                {"model": config.OLLAMA_MODEL, "messages": send_messages, "temperature": temperature},
                timeout=timeout + 30,
            )
            text = data["choices"][0]["message"]["content"].strip()
            if text or not vision_image:
                return text, errors
            errors.append("ollama returned empty vision response")
        except Exception as exc:
            errors.append(f"ollama: {exc}")
    raise RuntimeError("no language core reachable — " + "; ".join(errors or ["nothing configured"]))


def chat(messages: list[dict], temperature: float = 0.6, model: str = "") -> str:
    reply, _ = _route(messages, temperature, None, 60, model_override=model)
    return reply


def chat_vision(prompt: str, image_b64: str, temperature: float = 0.4) -> str:
    messages = [{"role": "user", "content": prompt}]
    reply, _ = _route(messages, temperature, image_b64, 90)
    return reply
