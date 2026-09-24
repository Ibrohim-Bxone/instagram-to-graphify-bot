import base64
import json
import logging
import os
import shutil
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)

API = "v1beta1"


def _get_project() -> str:
    return getattr(config, "VERTEX_PROJECT", "") or os.getenv("VERTEX_PROJECT", "")


def _get_location() -> str:
    return getattr(config, "VERTEX_LOCATION", "global") or os.getenv("VERTEX_LOCATION", "global")


def _get_gcloud() -> str:
    return getattr(config, "GCLOUD_PATH", "") or os.getenv("GCLOUD_PATH") or shutil.which("gcloud") or "gcloud"


# Tashqi muvofiqlik uchun modul darajasidagi o'zgaruvchilar
PROJ = _get_project()
LOC = _get_location()
GCLOUD = _get_gcloud()

# Oxirgi Vertex chaqiruvidan olingan usageMetadata
last_usage = {}


class VertexError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class VertexQuotaError(VertexError):
    def __init__(self, message: str = "Vertex quota exceeded (429)", status: int = 429):
        super().__init__(message, status=status)


def _get_token() -> str:
    gcloud = _get_gcloud()
    try:
        return subprocess.check_output(
            [gcloud, "auth", "application-default", "print-access-token"], text=True
        ).strip()
    except subprocess.CalledProcessError as e:
        raise VertexError(f"gcloud token failed: {e}") from e
    except FileNotFoundError as e:
        raise VertexError(f"gcloud not found: {e}") from e


def chat_json(messages: list, schema: dict) -> dict:
    global last_usage
    tok = _get_token()
    model = getattr(config, "VERTEX_MODEL", "gemini-3.7-flash")
    proj = _get_project()
    loc = _get_location()
    max_tokens = getattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 8192)
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{proj}"
        f"/locations/{loc}/publishers/google/models/{model}:generateContent"
    )

    system_text = ""
    user_parts = []
    
    for m in messages:
        if m["role"] == "system":
            system_text += m["content"] + "\n"
        elif m["role"] == "user":
            user_parts.append({"text": m["content"]})

    tana = {
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }
    }
    if system_text:
        tana["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(tana).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-user-project": proj,
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            raise VertexQuotaError(f"HTTP 429: {err_body}", status=429) from e
        raise VertexError(f"HTTP {e.code}: {err_body}", status=e.code) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    candidates = d.get("candidates", [])
    if candidates:
        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise VertexError("Vertex javobi kesildi (finishReason=MAX_TOKENS)")

    try:
        content = "".join(
            p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])
        ) if candidates else ""
        if not content:
            raise VertexError("Empty content from Vertex")
        res = json.loads(content)
        usage = d.get("usageMetadata", {})
        last_usage = usage
        if isinstance(res, dict) and usage:
            res["usageMetadata"] = usage
            res["prompt_tokens"] = usage.get("promptTokenCount", 0)
            res["candidates_tokens"] = usage.get("candidatesTokenCount", 0)
            res["thoughts_tokens"] = usage.get("thoughtsTokenCount", 0)
            res["total_tokens"] = usage.get("totalTokenCount", 0)
        return res
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise VertexError(f"Invalid JSON response from Vertex: {e}") from e


def chat_vision(messages: list, image_paths: list) -> str:
    """Send video frames and text prompts to Vertex AI Gemini model and return text response."""
    global last_usage
    tok = _get_token()
    model = getattr(config, "VERTEX_MODEL", "gemini-3.7-flash")
    proj = _get_project()
    loc = _get_location()
    max_tokens = getattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 8192)
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{proj}"
        f"/locations/{loc}/publishers/google/models/{model}:generateContent"
    )

    user_parts = []
    for p in image_paths:
        try:
            data_bytes = Path(p).read_bytes()
            b64 = base64.b64encode(data_bytes).decode("utf-8")
            user_parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": b64,
                }
            })
        except Exception as e:
            raise VertexError(f"Failed to read image {p}: {e}") from e

    system_text = ""
    for m in messages:
        if m["role"] == "system":
            system_text += m["content"] + "\n"
        elif m["role"] == "user":
            user_parts.append({"text": m["content"]})

    tana = {
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
        },
    }
    if system_text:
        tana["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(tana).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-user-project": proj,
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            raise VertexQuotaError(f"HTTP 429: {err_body}", status=429) from e
        raise VertexError(f"HTTP {e.code}: {err_body}", status=e.code) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    candidates = d.get("candidates", [])
    if candidates:
        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise VertexError("Vertex javobi kesildi (finishReason=MAX_TOKENS)")

    try:
        content = "".join(
            p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])
        ) if candidates else ""
        if not content:
            raise VertexError("Empty content from Vertex")
        last_usage = d.get("usageMetadata", {})
        return content.strip()
    except (KeyError, IndexError) as e:
        raise VertexError(f"Invalid response from Vertex: {e}") from e


def verify_tool(name: str) -> dict:
    global last_usage
    tok = _get_token()
    model = getattr(config, "VERTEX_MODEL", "gemini-3.7-flash")
    proj = _get_project()
    loc = _get_location()
    max_tokens = getattr(config, "VERTEX_MAX_OUTPUT_TOKENS", 8192)
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{proj}"
        f"/locations/{loc}/publishers/google/models/{model}:generateContent"
    )

    tana = {
        "contents": [{"role": "user", "parts": [{"text": f"Mavjud dasturiy ta'minot, AI vosita yoki platforma bormi: '{name}'? Agar mavjud bo'lsa, uning rasmiy sahifasi yoki asosiy manba havolasini qaytar. Boshqa hech narsa yozma."}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "found": {"type": "boolean"},
                    "url": {"type": "string"}
                },
                "required": ["found", "url"]
            }
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(tana).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-user-project": proj,
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            raise VertexQuotaError(f"HTTP 429: {err_body}", status=429) from e
        raise VertexError(f"HTTP {e.code}: {err_body}", status=e.code) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    candidates = d.get("candidates", [])
    if candidates:
        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS":
            raise VertexError("Vertex javobi kesildi (finishReason=MAX_TOKENS)")

    try:
        content = "".join(
            p.get("text", "") for p in candidates[0].get("content", {}).get("parts", [])
        ) if candidates else ""
        if not content:
            raise VertexError("Empty content from Vertex")
        last_usage = d.get("usageMetadata", {})
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise VertexError(f"Invalid response from Vertex: {e}") from e

