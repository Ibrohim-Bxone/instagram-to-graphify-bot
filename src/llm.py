"""Thin Ollama client. Local models only — no API tokens are spent at runtime."""

import base64
import json
import logging
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class TruncatedResponseError(LLMError):
    """Raised when Ollama stops generation early (e.g. done_reason='length')."""
    def __init__(self, message: str, raw: str = "", done_reason: str = ""):
        super().__init__(message)
        self.raw = raw
        self.done_reason = done_reason


def chat(model: str, messages: list, schema: dict | None = None,
         images: list | None = None, timeout: int = 1800,
         num_predict: int | None = None, num_ctx: int | None = None) -> str:
    """One chat turn. `schema` uses Ollama's structured-output mode: without it a
    12B model reliably breaks JSON with stray prose or trailing commas."""
    if images:
        messages = list(messages)
        messages[-1] = dict(messages[-1])
        messages[-1]["images"] = [
            base64.b64encode(Path(p).read_bytes()).decode() for p in images
        ]

    ctx = num_ctx if num_ctx is not None else config.OLLAMA_NUM_CTX
    predict = num_predict if num_predict is not None else config.OLLAMA_NUM_PREDICT

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
        # gemma4 has a "thinking" mode; left on, its reasoning trace has leaked
        # straight into structured JSON fields (seen in production) instead of
        # staying in the separate `thinking` response field. We never want it.
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": ctx, "num_predict": predict},
    }
    if schema:
        payload["format"] = schema

    try:
        r = requests.post(f"{config.OLLAMA_HOST}/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"Ollama request failed: {e}") from e

    body = r.json()
    done_reason = body.get("done_reason")
    content = (body.get("message") or {}).get("content", "").strip()
    if done_reason not in (None, "stop"):
        # "length" means Ollama cut generation short (num_ctx/num_predict budget
        # exhausted) — raising lets callers like chat_json detect premature
        # termination and retry with a larger budget instead of repeating blind.
        log.warning("Ollama stopped early (done_reason=%s, prompt_eval_count=%s, eval_count=%s)",
                    done_reason, body.get("prompt_eval_count"), body.get("eval_count"))
        raise TruncatedResponseError(
            f"Ollama stopped early (done_reason={done_reason})",
            raw=content,
            done_reason=done_reason or "",
        )
    return content


def chat_json(model: str, messages: list, schema: dict, **kw) -> dict:
    truncated = False
    raw = ""
    try:
        raw = chat(model, messages, schema=schema, **kw)
        return json.loads(raw)
    except TruncatedResponseError as e:
        truncated = True
        raw = e.raw
    except json.JSONDecodeError:
        pass

    if truncated:
        # Length budget was exhausted mid-generation: retrying with identical
        # parameters would fail again. Scale up both context and output token limits.
        log.warning("chat_json: response truncated on first attempt (%d chars), retrying with expanded budget",
                    len(raw))
        ctx = int((kw.get("num_ctx") or config.OLLAMA_NUM_CTX) * 1.5)
        predict = int((kw.get("num_predict") or config.OLLAMA_NUM_PREDICT) * 1.5)
        retry_kw = {**kw, "num_ctx": ctx, "num_predict": predict}
    else:
        # The model finished normally but returned invalid JSON. Standard retry
        # with original parameters to let temperature variation fix stray tokens.
        log.warning("chat_json: non-JSON response on first attempt (%d chars), retrying once. Full response:\n%s",
                    len(raw), raw)
        retry_kw = kw

    try:
        raw = chat(model, messages, schema=schema, **retry_kw)
        return json.loads(raw)
    except TruncatedResponseError as e:
        raw = e.raw
        log.warning("chat_json: response truncated on retry too (%d chars). Full response:\n%s",
                    len(raw), raw)
        raise LLMError(f"model returned non-JSON: {raw[:300]}") from e
    except json.JSONDecodeError as e:
        log.warning("chat_json: non-JSON response on retry too (%d chars). Full response:\n%s",
                    len(raw), raw)
        raise LLMError(f"model returned non-JSON: {raw[:300]}") from e


def unload(model: str) -> None:
    """Ask Ollama to drop the model from VRAM (keep_alive=0)."""
    try:
        requests.post(f"{config.OLLAMA_HOST}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=60)
    except requests.RequestException:
        pass
