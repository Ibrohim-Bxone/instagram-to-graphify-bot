"""Thin Ollama client. Local models only — no API tokens are spent at runtime."""

import base64
import json
from pathlib import Path

import requests

from . import config


class LLMError(RuntimeError):
    pass


def chat(model: str, messages: list, schema: dict | None = None,
         images: list | None = None, timeout: int = 1800) -> str:
    """One chat turn. `schema` uses Ollama's structured-output mode: without it a
    12B model reliably breaks JSON with stray prose or trailing commas."""
    if images:
        messages = list(messages)
        messages[-1] = dict(messages[-1])
        messages[-1]["images"] = [
            base64.b64encode(Path(p).read_bytes()).decode() for p in images
        ]

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        # gemma4 has a "thinking" mode; left on, its reasoning trace has leaked
        # straight into structured JSON fields (seen in production) instead of
        # staying in the separate `thinking` response field. We never want it.
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": config.OLLAMA_NUM_CTX},
    }
    if schema:
        payload["format"] = schema

    try:
        r = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    return (r.json().get("message") or {}).get("content", "").strip()


def chat_json(model: str, messages: list, schema: dict, **kw) -> dict:
    raw = chat(model, messages, schema=schema, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Long inputs can make Ollama stop mid-object. Retry once before surfacing the failure.
        raw = chat(model, messages, schema=schema, **kw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"model returned non-JSON: {raw[:300]}") from e


def unload(model: str) -> None:
    """Ask Ollama to drop the model from VRAM (keep_alive=0)."""
    try:
        requests.post(f"{config.OLLAMA_HOST}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=60)
    except requests.RequestException:
        pass
