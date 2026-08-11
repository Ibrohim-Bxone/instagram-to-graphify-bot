"""Markdown archive — the source of truth.

The vector DB is a derived index: it can be rebuilt from these files at any time
(see reindex.py), which is what makes swapping the embedding model or recovering
from a corrupted store a one-command job. Each file therefore carries the full
extraction record as JSON, not just the prose rendering of it.
"""

import json
import re
from datetime import date
from pathlib import Path

from . import config

_JSON_BLOCK = re.compile(r"<!-- graphify-record\s*```json\s*(.*?)\s*```\s*-->", re.DOTALL)


def path_for(shortcode: str) -> Path:
    return config.ARCHIVE_DIR / f"{shortcode}.md"


def _yaml_escape(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(record: dict) -> str:
    d = record
    tags = ", ".join(d.get("tags_en", []))
    lines = [
        "---",
        f"title: {_yaml_escape(d.get('title_en', ''))}",
        f"shortcode: {d.get('shortcode', '')}",
        f"url: {d.get('url', '')}",
        f"uploader: {_yaml_escape(d.get('uploader', ''))}",
        f"date: {d.get('date', '')}",
        f"content_type: {d.get('content_type', 'other')}",
        f"tags: {_yaml_escape(tags)}",
        f"spoken_language: {d.get('language', '')}",
        f"usable: {str(bool(d.get('usable', False))).lower()}",
        f"flags: {json.dumps(d.get('flags', []), ensure_ascii=False)}",
        "---",
        "",
        f"# {d.get('title_en') or d.get('shortcode', 'Video')}",
        "",
    ]
    if d.get("url"):
        lines += [f"**Manba:** {d['url']}", ""]

    if d.get("summary_uz"):
        lines += ["## Xulosa", ""] + [f"- {s}" for s in d["summary_uz"]] + [""]

    if d.get("items"):
        lines += ["## Ajratilgan promptlar va skilllar", ""]
        for i, item in enumerate(d["items"], 1):
            if item.get("kind") != "prompt":
                mark = ""  # verbatim-fidelity is only meaningful for quoted prompts
            else:
                mark = "✅ so'zma-so'z" if item.get("verified") else "⚠️ tasdiqlanmagan"
            lines += [f"### {i}. {item.get('name_en', '?')} — `{item.get('kind', '?')}` {mark}",
                      "", "```text", item.get("content", "").strip(), "```", ""]
            if item.get("note_uz"):
                lines += [f"_Izoh:_ {item['note_uz']}", ""]

    if d.get("apply_suggestions_uz"):
        lines += ["## Loyihalarda qo'llash — tavsiya (qo'llanilmagan)", ""]
        lines += [f"- {s}" for s in d["apply_suggestions_uz"]] + [""]

    if d.get("links"):
        lines += ["## Havolalar", ""] + [f"- {u}" for u in d["links"]] + [""]

    if d.get("onscreen"):
        lines += ["## Kadrlardagi matn", "", d["onscreen"].strip(), ""]

    if d.get("transcript"):
        lines += ["## Transkript", "", d["transcript"].strip(), ""]

    if d.get("caption"):
        lines += ["## Instagram caption", "", d["caption"].strip(), ""]

    lines += ["<!-- graphify-record", "```json",
              json.dumps(record, ensure_ascii=False, indent=2), "```", "-->", ""]
    return "\n".join(lines)


def write(record: dict) -> Path:
    record.setdefault("date", date.today().isoformat())
    p = path_for(record["shortcode"])
    p.write_text(render(record), encoding="utf-8")
    return p


def read(path: Path) -> dict | None:
    m = _JSON_BLOCK.search(path.read_text(encoding="utf-8"))
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def all_records():
    for p in sorted(config.ARCHIVE_DIR.glob("*.md")):
        rec = read(p)
        if rec:
            yield p, rec
