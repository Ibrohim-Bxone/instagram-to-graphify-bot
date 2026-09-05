import base64
import json
import logging
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from . import config

log = logging.getLogger(__name__)

PROJ = "project-92b77c1d-c511-47ce-965"
LOC = "global"
API = "v1beta1"
GCLOUD = r"C:\Users\user\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"


class VertexError(RuntimeError):
    pass


def _get_token() -> str:
    try:
        return subprocess.check_output(
            [GCLOUD, "auth", "application-default", "print-access-token"], text=True
        ).strip()
    except subprocess.CalledProcessError as e:
        raise VertexError(f"gcloud token failed: {e}") from e
    except FileNotFoundError as e:
        raise VertexError(f"gcloud not found: {e}") from e


def chat_json(messages: list, schema: dict) -> dict:
    tok = _get_token()
    model = config.VERTEX_MODEL
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{PROJ}"
        f"/locations/{LOC}/publishers/google/models/{model}:generateContent"
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
            "x-goog-user-project": PROJ,
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise VertexError(f"HTTP {e.code}: {err_body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    try:
        content = "".join(
            p.get("text", "") for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        )
        if not content:
            raise VertexError("Empty content from Vertex")
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise VertexError(f"Invalid JSON response from Vertex: {e}") from e


def chat_vision(messages: list, image_paths: list) -> str:
    """Send video frames and text prompts to Vertex AI Gemini model and return text response."""
    tok = _get_token()
    model = config.VERTEX_MODEL
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{PROJ}"
        f"/locations/{LOC}/publishers/google/models/{model}:generateContent"
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
            "x-goog-user-project": PROJ,
        },
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise VertexError(f"HTTP {e.code}: {err_body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    try:
        content = "".join(
            p.get("text", "") for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        )
        if not content:
            raise VertexError("Empty content from Vertex")
        return content.strip()
    except (KeyError, IndexError) as e:
        raise VertexError(f"Invalid response from Vertex: {e}") from e


def verify_tool(name: str) -> dict:
    tok = _get_token()
    model = config.VERTEX_MODEL
    url = (
        f"https://aiplatform.googleapis.com/{API}/projects/{PROJ}"
        f"/locations/{LOC}/publishers/google/models/{model}:generateContent"
    )

    tana = {
        "contents": [{"role": "user", "parts": [{"text": f"Mavjud dasturiy ta'minot, AI vosita yoki platforma bormi: '{name}'? Agar mavjud bo'lsa, uning rasmiy sahifasi yoki asosiy manba havolasini qaytar. Boshqa hech narsa yozma."}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.0,
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
            "x-goog-user-project": PROJ,
        }
    )

    try:
        resp = urllib.request.urlopen(req, timeout=config.VERTEX_TIMEOUT)
        d = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise VertexError(f"HTTP {e.code}: {err_body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise VertexError(f"Network error: {e}") from e
    except Exception as e:
        raise VertexError(f"Unexpected error: {e}") from e

    try:
        content = "".join(
            p.get("text", "") for p in d.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        )
        if not content:
            raise VertexError("Empty content from Vertex")
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise VertexError(f"Invalid response from Vertex: {e}") from e

