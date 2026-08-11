"""Turning raw signal (transcript + caption + on-screen text) into structured knowledge."""

import difflib
import re

from . import config, llm

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "content_type": {
            "type": "string",
            "enum": ["skill", "prompt", "tool", "news", "technique", "other"],
        },
        "summary_uz": {"type": "array", "items": {"type": "string"}},
        "tags_en": {"type": "array", "items": {"type": "string"}},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["prompt", "skill", "tool"]},
                    "name_en": {"type": "string"},
                    "content": {"type": "string"},
                    "note_uz": {"type": "string"},
                },
                "required": ["kind", "name_en", "content", "note_uz"],
            },
        },
        "apply_suggestions_uz": {"type": "array", "items": {"type": "string"}},
        "usable": {"type": "boolean"},
    },
    "required": ["title_en", "content_type", "summary_uz", "tags_en", "items",
                 "apply_suggestions_uz", "usable"],
}

_SYSTEM = """You extract reusable knowledge from short social-media videos about AI,
prompting, developer tooling and tech news.

Rules:
- title_en, tags_en, and item name_en MUST be English. They are the retrieval anchors
  for an English-only embedding model; Uzbek there would make the entry unfindable.
- summary_uz, note_uz and apply_suggestions_uz MUST be Uzbek (o'zbek tilida).
- items: every concrete prompt, skill/workflow idea or named tool mentioned. Copy a
  prompt VERBATIM in its original language. Never translate, shorten or rewrite a
  prompt — a rewritten prompt is worthless to the user.
- apply_suggestions_uz: 1-3 concrete suggestions for how this could be applied in the
  user's own projects. These are SUGGESTIONS ONLY. Never phrase them as done actions.
- usable: false if the source text is empty, unintelligible, or pure entertainment
  with no reusable technical content. Do not invent content to fill the fields.
"""

_VISION_SYSTEM = """You read frames from a short video. Respond in exactly this shape:

TEXT:
<all on-screen text from every frame, transcribed VERBATIM, in reading order, in its
original language, with nothing else mixed in — no descriptions, no frame numbers,
no commentary between lines. If a frame has no text, skip it silently.>

SCENE:
<one sentence describing what is shown overall.>

Never guess at unreadable text. Keeping the TEXT block as pure verbatim transcript
matters: it gets compared word-for-word against the extracted prompts later."""


def read_frames(frame_paths: list) -> str:
    if not frame_paths:
        return ""
    msg = [{"role": "system", "content": _VISION_SYSTEM},
           {"role": "user", "content": "Read these frames."}]
    return llm.chat(config.MODEL_VISION, msg, images=frame_paths)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def verify_item(content: str, source_text: str) -> bool:
    """Is this item actually grounded in the source, or did the model write it?

    Local models paraphrase prompts even when told not to; an unverified prompt
    gets flagged rather than silently trusted. Two checks, either is enough:
    a long contiguous match (catches verbatim copies split across a paraphrase),
    or high word overlap (catches text that is genuinely present but was split
    across separate vision frames/lines, so it is not contiguous in the source).
    """
    needle, hay = _normalize(content), _normalize(source_text)
    if len(needle) < 15:
        return needle in hay

    match = difflib.SequenceMatcher(None, needle, hay).find_longest_match(
        0, len(needle), 0, len(hay))
    if match.size / len(needle) >= 0.6:
        return True

    needle_words = re.findall(r"\w+", needle)
    hay_words = set(re.findall(r"\w+", hay))
    if not needle_words:
        return False
    overlap = sum(1 for w in needle_words if w in hay_words) / len(needle_words)
    return overlap >= 0.85


def extract(transcript: str, caption: str, onscreen: str, meta: dict) -> dict:
    source_parts = []
    if caption:
        source_parts.append(f"### Instagram caption\n{caption}")
    if onscreen:
        source_parts.append(f"### On-screen text (from video frames)\n{onscreen}")
    if transcript:
        source_parts.append(f"### Spoken transcript\n{transcript}")
    source_text = "\n\n".join(source_parts)

    if not source_text.strip():
        return {"title_en": "", "content_type": "other", "summary_uz": [], "tags_en": [],
                "items": [], "apply_suggestions_uz": [], "usable": False,
                "source_text": "", "flags": ["no_source_text"]}

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Author: {meta.get('uploader') or 'unknown'}\n"
                                    f"Duration: {meta.get('duration') or '?'}s\n\n{source_text}"},
    ]
    data = llm.chat_json(config.MODEL_EXTRACT, messages, _EXTRACT_SCHEMA)

    flags = []
    for item in data.get("items", []):
        # Verbatim-fidelity checking only makes sense for "prompt": that's the one
        # kind where the model was told to quote, not describe. A "skill"/"tool"
        # entry is always the model's own characterization of what it saw — running
        # the same strict check there flags nearly every one and drowns the signal
        # for prompts that were genuinely paraphrased instead of copied.
        if item.get("kind") == "prompt":
            item["verified"] = verify_item(item.get("content", ""), source_text)
            if not item["verified"]:
                flags.append(f"unverified:{item.get('name_en', '?')}")
        else:
            item["verified"] = True

    data["source_text"] = source_text
    data["flags"] = flags
    return data
