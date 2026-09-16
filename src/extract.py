"""Turning raw signal (transcript + caption + on-screen text) into structured knowledge."""

import difflib
import json
import logging
import os
import re
import time

from . import config, llm

log = logging.getLogger(__name__)

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


_last_vision_fallback: bool = False


def _ensure_tracker_patched() -> None:
    """_OlchovTracker ga vision_backend maydonini qo'shish (pipeline.py ni o'zgartirmasdan)."""
    import sys
    pipeline_mod = sys.modules.get("src.pipeline")
    if pipeline_mod and hasattr(pipeline_mod, "_OlchovTracker"):
        tracker_cls = pipeline_mod._OlchovTracker
        if not getattr(tracker_cls, "_vision_patched", False):
            orig_init = tracker_cls.__init__
            orig_update = tracker_cls.update_from_record
            orig_to_dict = tracker_cls.to_dict

            def new_init(self, shortcode, source_type):
                orig_init(self, shortcode, source_type)
                self.vision_backend = (
                    "vertex"
                    if getattr(config, "VISION_BACKEND", "vertex") == "vertex"
                    else getattr(config, "MODEL_VISION", "gemma4:12b")
                )

            def new_update(self, record):
                orig_update(self, record)
                flags = record.get("flags") or []
                if "vision: ollama-fallback" in flags:
                    self.vision_backend = "ollama-fallback"
                elif getattr(config, "VISION_BACKEND", "vertex") == "vertex":
                    self.vision_backend = "vertex"
                else:
                    self.vision_backend = getattr(config, "MODEL_VISION", "gemma4:12b")

            def new_to_dict(self):
                d = orig_to_dict(self)
                d["vision_backend"] = getattr(self, "vision_backend", "vertex")
                return d

            tracker_cls.__init__ = new_init
            tracker_cls.update_from_record = new_update
            tracker_cls.to_dict = new_to_dict
            tracker_cls._vision_patched = True


def _read_frames_ollama(frame_paths: list, msg: list) -> str:
    """Ollama ba'zan `done_reason=length` bilan to'xtaydi va kadrlarning yarmini
    qaytaradi. Kattaroq budjet bilan bir marta qayta so'rash ularni tiklaydi;
    ikkinchisi ham kesilsa qisman matn saqlanadi — GPU ni ikki marta sarflab
    bo'sh qaytish eng yomon natija."""
    try:
        return llm.chat(config.MODEL_VISION, msg, images=frame_paths)
    except llm.TruncatedResponseError as first:
        log.warning("Vision javobi kesildi (%s) — kattaroq budjet bilan qayta urinilmoqda", first)
        try:
            return llm.chat(config.MODEL_VISION, msg, images=frame_paths,
                            num_predict=config.OLLAMA_NUM_PREDICT * 2,
                            num_ctx=config.OLLAMA_NUM_CTX * 2)
        except llm.TruncatedResponseError as second:
            partial = (second.raw or first.raw or "").strip()
            if not partial:
                raise
            log.warning("Vision ikki marta kesildi — qisman matn saqlanadi")
            return partial


def read_frames(frame_paths: list, flags: list | None = None) -> str:
    global _last_vision_fallback
    _ensure_tracker_patched()
    if not frame_paths:
        _last_vision_fallback = False
        return ""
    msg = [{"role": "system", "content": _VISION_SYSTEM},
           {"role": "user", "content": "Read these frames."}]

    backend = getattr(config, "VISION_BACKEND", "vertex").lower()
    if backend == "vertex":
        from . import vertex
        import time
        result = None
        for attempt in range(2):
            try:
                result = vertex.chat_vision(msg, frame_paths)
                break
            except vertex.VertexError as e:
                log.warning("Vertex vision urunishi %d muvaffaqiyatsiz: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(2)

        if result is None:
            log.warning("⚠️ Vertex vision ishlamadi (barcha urinishlar barbod) — %s ga o'tildi", config.MODEL_VISION)
            result = _read_frames_ollama(frame_paths, msg)
            _last_vision_fallback = True
            if flags is not None:
                flags.append("vision: ollama-fallback")
        else:
            _last_vision_fallback = False
        return result
    else:
        _last_vision_fallback = False
        return _read_frames_ollama(frame_paths, msg)


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


def _clean_items(items: list) -> None:
    """Elementlarni normallashtirish: bo'sh nom, ortiqcha bo'shliq, dublikat nom."""
    seen_names = set()
    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name_en") or "")
        name = re.sub(r"\s+", " ", raw_name).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        item["name_en"] = name
        cleaned.append(item)
    items[:] = cleaned


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
    if config.EXTRACT_BACKEND == "vertex":
        from . import vertex
        data = None
        for attempt in range(2):
            try:
                data = vertex.chat_json(messages, _EXTRACT_SCHEMA)
                break
            except vertex.VertexError as e:
                log.warning("Vertex urunishi %d muvaffaqiyatsiz: %s", attempt + 1, e)
                if attempt == 0:
                    time.sleep(2)
        
        if data is None:
            log.warning("⚠️ Vertex ishlamadi (barcha urinishlar barbod) — %s ga o'tildi", config.MODEL_EXTRACT)
            data = llm.chat_json(config.MODEL_EXTRACT, messages, _EXTRACT_SCHEMA)
            fallback_flag = "llm: gemma4-fallback"
        else:
            fallback_flag = None
    else:
        data = llm.chat_json(config.MODEL_EXTRACT, messages, _EXTRACT_SCHEMA)
        fallback_flag = None

    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    data["items"] = [i for i in data["items"] if isinstance(i, dict)]
    _clean_items(data["items"])

    flags = [fallback_flag] if fallback_flag else []
    global _last_vision_fallback
    if _last_vision_fallback:
        flags.append("vision: ollama-fallback")
        _last_vision_fallback = False
    # Havola tekshirish bloki (GitHub link verify)
    if getattr(config, "LINK_VERIFY", True):
        try:
            from . import link_verify
            link_verify.verify_links_in_items(
                items=data["items"],
                source_text=source_text,
                title=data.get("title_en", ""),
            )
        except Exception as e:
            log.warning("Havolani tekshirishda xatolik (konveyer davom etadi): %s", e)

    # Nom tekshirish bloki
    if getattr(config, "VERIFY_NAMES", True):
        cache_path = config.DATA_DIR / "tools_cache.json"
        tools_cache = {}
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    tools_cache = json.load(f)
            except Exception:
                tools_cache = {}

        cache_updated = False
        for item in data["items"]:
            if item.get("kind") != "prompt":
                name = item.get("name_en", "")
                if not name:
                    continue
                if item.get("source_url"):
                    continue
                if item.get("rate_limited"):
                    continue
                if name in tools_cache:
                    cached = tools_cache[name]
                    if isinstance(cached, dict):
                        if cached.get("found"):
                            item["source_url"] = cached.get("url")
                            continue
                        elif cached.get("status") == "error":
                            # Backend butunlay ishlamayotganda har nomga bayroq
                            # qo'yish kartochkani shovqin bilan to'ldiradi va
                            # nomni haqiqatan topilmagandek ko'rsatadi.
                            if fallback_flag:
                                continue
                            err_ts = cached.get("timestamp")
                            ttl = getattr(config, "TOOLS_CACHE_ERROR_TTL_SEC", 86400)
                            if isinstance(err_ts, (int, float)) and (time.time() - err_ts < ttl):
                                flags.append(f"unchecked:{name}")
                                continue
                        else:
                            flags.append(f"unverified:{name}")
                            continue

                # Keshda yo'q yoki eskirgan xatolik — tekshirish kerak
                if config.EXTRACT_BACKEND == "vertex" and not fallback_flag:
                    from . import vertex
                    try:
                        time.sleep(1)
                        res = vertex.verify_tool(name)
                        if res.get("found"):
                            item["source_url"] = res.get("url")
                            tools_cache[name] = {"found": True, "url": res.get("url")}
                        else:
                            flags.append(f"unverified:{name}")
                            tools_cache[name] = {"found": False}
                        cache_updated = True
                    except Exception as e:
                        log.warning("Tool verification failed for %s: %s", name, e)
                        flags.append(f"unchecked:{name}")
                        tools_cache[name] = {"status": "error", "timestamp": time.time()}
                        cache_updated = True
                else:
                    # Tekshiruv imkoni yo'q (backend yiqilgan yoki Vertex emas) —
                    # jim o'tkaziladi, bayroq qo'yilmaydi.
                    pass

        if cache_updated:
            try:
                tmp_file = cache_path.with_suffix(".tmp")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(tools_cache, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, cache_path)
            except Exception as e:
                log.warning("Keshni saqlashda xatolik: %s", e)

    for item in data.get("items", []):
        if item.get("kind") == "prompt":
            item["verified"] = verify_item(item.get("content", ""), source_text)
            if not item["verified"]:
                flags.append(f"unverified:{item.get('name_en', '?')}")
        else:
            if getattr(config, "VERIFY_NAMES", True):
                item["verified"] = bool(item.get("source_url"))
            else:
                item["verified"] = True

    # `rate_limited` — VAQTINCHALIK holat ("bu safar GitHub limiti tufayli
    # tekshira olmadik"), arxivga yozilmasligi kerak: arxiv doimiy yozuv va
    # limit o'tib ketgandan keyin ham u yerda muzlab qolardi. Nom tekshirish
    # bloki uni yuqorida allaqachon o'qib bo'ldi.
    for item in data.get("items", []):
        item.pop("rate_limited", None)

    data["source_text"] = source_text
    data["flags"] = flags
    return data
