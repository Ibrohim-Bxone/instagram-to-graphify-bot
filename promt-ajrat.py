#!/usr/bin/env python3
"""
promt-ajrat.py

Instagram bot arxividagi (Promtlarim/instagram) prompt bloklarini ajratib,
turkumlar bo'yicha Promtlarim/prompts/ qolipida yozish uchun skript.

Foydalanish:
    python promt-ajrat.py --quruq
    python promt-ajrat.py --arxiv "yo'l" --chiqish "yo'l" [--quruq]
"""

import argparse
import glob
import hashlib
import json
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):      # Windows konsoli cp1251 —
    sys.stdout.reconfigure(encoding="utf-8")  # o'zbekcha matn yiqitmasin



def parse_frontmatter(content: str) -> dict:
    """YAML frontmatter qismini sodda kalit-qiymat shaklida ajratib oladi."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    fm = {}
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def slugify(text: str) -> str:
    """Sarlavhadan lotin harflarda, kichik, _ bilan fayl nomi yaratadi."""
    text = text.lower().strip()
    text = re.sub(r"['\"]", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text or "promt"


def determine_category(item: dict, tags, title: str):
    """
    Blokning subtype, tags va sarlavhasiga qarab toifani aniqlaydi.
    Mavjud toifalar: dasturlash_va_ai, vebsayt_va_kopirayting, samaradorlik.
    Yangi ochiladigan toifalar: video_va_media, biznes_va_moliya.
    """
    subtype = (item.get("subtype") or "").lower()
    name = (item.get("name_en") or "").lower()
    if isinstance(tags, list):
        tags_str = " ".join(str(t).lower() for t in tags)
    else:
        tags_str = str(tags).lower()
    combined = f"{subtype} {name} {tags_str} {title.lower()}"

    # 1. Video va Media
    if subtype in ["video-generation", "image-generation"] or any(
        k in combined
        for k in [
            "video editing",
            "poster prompt",
            "midjourney",
            "dalle",
            "reels producer",
            "video",
        ]
    ):
        return "video_va_media", "Video va Media"

    # 2. Dasturlash va AI
    if subtype in ["coding", "system-prompt"] or any(
        k in combined
        for k in [
            "coding",
            "code review",
            "sql",
            "developer",
            "claude code",
            "harness",
            "system prompt",
            "llm optimization",
            "context window",
            "benchmark",
            "fable 5",
            "reasoning structure",
        ]
    ):
        return "dasturlash_va_ai", "Dasturlash va AI"

    # 3. Vebsayt va Kopirayting
    if subtype in ["content-writing", "marketing"] or any(
        k in combined
        for k in [
            "copywriting",
            "landing page",
            "instagram comment",
            "content writing",
            "content creation",
            "content strategy",
            "digital product",
        ]
    ):
        return "vebsayt_va_kopirayting", "Vebsayt va Kopirayting"

    # 4. Biznes va Moliya
    if any(
        k in combined
        for k in [
            "trading",
            "finance",
            "financial",
            "order book",
            "wealth",
            "investment",
            "deal",
            "negotiation",
            "lead generation",
            "pricing comparison",
        ]
    ):
        return "biznes_va_moliya", "Biznes va Moliya"

    # 5. Samaradorlik va Ta'lim (default fallback)
    return "samaradorlik", "Samaradorlik va Ta'lim"


def format_prompt_file(p: dict) -> str:
    """Fayl matnini berilgan qolip asosida shakllantiradi."""
    lines = [f"# {p['title']}\n"]
    lines.append(f"**Turkum:** {p['category_name']}")
    lines.append(f"**Maqsad:** {p['target']}")
    lines.append(f"**Manba:** {p['source']}")
    if p["unverified"]:
        lines.append("**⚠️ TEKSHIRILMAGAN**")
    lines.append("\n## Promt:")

    content_lines = p["content"].splitlines()
    quoted = []
    for cl in content_lines:
        if cl.strip():
            quoted.append(f"> {cl}")
        else:
            quoted.append(">")
    lines.append("\n".join(quoted))
    return "\n".join(lines) + "\n"


def parse_markdown_blocks(content: str) -> list:
    """Agar graphify-record bo'lmasa, markdown matnidan bloklarni ajratish fallbacki."""
    pattern = re.compile(
        r"###\s+\d+\.\s+(.*?)\s+—\s+`([^/`]+)(?:\s*/\s*([^`]+))?`.*?"
        r"```(?:text|markdown)?\s*\n(.*?)\n```.*?"
        r"(?:_Izoh:_\s*(.*?)(?=\n###|\n##|\Z))?",
        re.DOTALL,
    )
    section = content
    if "## Ajratilgan promptlar va skilllar" in content:
        section = content.split("## Ajratilgan promptlar va skilllar")[1]
    if "## Loyihalarda qo'llash" in section:
        section = section.split("## Loyihalarda qo'llash")[0]
    elif "<!-- graphify-record" in section:
        section = section.split("<!-- graphify-record")[0]

    items = []
    for m in pattern.finditer(section):
        items.append(
            {
                "name_en": m.group(1).strip(),
                "kind": m.group(2).strip().lower(),
                "subtype": (m.group(3) or "other").strip().lower(),
                "content": (m.group(4) or "").strip(),
                "note_uz": (m.group(5) or "").strip(),
                "verified": True,
            }
        )
    return items


def main():
    parser = argparse.ArgumentParser(
        description="Instagram bot arxividagi promptlarni ajratish va toifalash."
    )
    parser.add_argument(
        "--arxiv",
        type=str,
        default=r"D:\claude projects\graphify-ekotizim\Promtlarim\instagram",
        help="Instagram arxiv papkasi yo'li",
    )
    parser.add_argument(
        "--chiqish",
        type=str,
        default=r"D:\claude projects\graphify-ekotizim\Promtlarim\prompts",
        help="Chiqish papkasi yo'li (Promtlarim/prompts)",
    )
    parser.add_argument(
        "--quruq",
        action="store_true",
        help="Quruq rejim: fayllarni yozmasdan faqat hisobot va namunalarni ko'rsatadi",
    )

    args = parser.parse_args()

    arxiv_dir = Path(args.arxiv)
    chiqish_dir = Path(args.chiqish)

    if not arxiv_dir.exists():
        print(f"XATO: Arxiv papkasi topilmadi: {arxiv_dir}", file=sys.stderr)
        sys.exit(1)

    # Mavjud fayllar ro'yxatini yuklaymiz (ustiga yozmaslik uchun)
    existing_files_all = set()
    if chiqish_dir.exists():
        for f in chiqish_dir.rglob("*.md"):
            existing_files_all.add(f.name.lower())

    files = sorted(arxiv_dir.glob("*.md"))

    total_blocks = 0
    total_prompt_blocks = 0
    usable_false_skipped = 0
    empty_content_skipped = 0
    duplicates_skipped = 0

    seen_hashes = set()
    planned_files = []
    category_counts = {}
    new_categories = set()
    existing_categories = {"dasturlash_va_ai", "samaradorlik", "vebsayt_va_kopirayting"}

    # Har bir toifa bo'yicha band bo'lgan fayl nomlari
    used_filenames = {cat: set() for cat in existing_categories}

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as fp:
            content = fp.read()

        fm = parse_frontmatter(content)

        # JSON yoki Markdown bloklarini o'qiymiz
        m_json = re.search(
            r"<!-- graphify-record\s*```json\s*(.*?)\s*```\s*-->",
            content,
            re.DOTALL,
        )
        data = {}
        if m_json:
            try:
                data = json.loads(m_json.group(1))
                items = data.get("items", [])
            except Exception:
                items = parse_markdown_blocks(content)
        else:
            items = parse_markdown_blocks(content)

        total_blocks += len(items)

        # Usable tekshiruvi (usable: false bo'lsa o'tkazib yuboriladi)
        is_usable_false = (fm.get("usable") == "false") or (
            data.get("usable") is False
        )

        # Unverified flag tekshiruvi
        post_flags = data.get("flags", [])
        if isinstance(post_flags, str):
            post_flags = [post_flags]
        fm_flags = fm.get("flags", "")
        post_unverified = any(
            "unverified" in str(fl).lower() for fl in post_flags
        ) or ("unverified" in str(fm_flags).lower())

        for it in items:
            kind = str(it.get("kind", "")).strip().lower()
            if kind != "prompt":
                continue

            total_prompt_blocks += 1

            if is_usable_false:
                usable_false_skipped += 1
                continue

            raw_content = it.get("content", "")
            content_clean = raw_content.strip()

            # Matn xeshi bo'yicha takrorlarni aniqlash (bir xil matnlar)
            content_hash = hashlib.sha256(content_clean.encode("utf-8")).hexdigest()
            if content_hash in seen_hashes:
                duplicates_skipped += 1
                continue
            seen_hashes.add(content_hash)

            # Matnsiz (bo'sh) promptlar — fayl qilib yozilmaydi
            if not content_clean:
                empty_content_skipped += 1
                continue

            # Unverified holati
            is_unverified = post_unverified or (it.get("verified") is False)

            # Sarlavha
            name = it.get("name_en", "").strip()
            if not name or name.upper() == "N/A":
                name = data.get("title_en") or fm.get("title", "Promt")

            # Toifa
            tags = data.get("tags_en") or fm.get("tags", "")
            title_text = data.get("title_en") or fm.get("title", "")
            cat_dir, cat_name = determine_category(it, tags, title_text)

            if cat_dir not in used_filenames:
                used_filenames[cat_dir] = set()

            if cat_dir not in existing_categories:
                new_categories.add(cat_dir)

            # Fayl nomini aniqlash (to'qnashuvsiz)
            base_slug = slugify(name)
            filename = f"{base_slug}.md"
            counter = 2
            while (
                filename.lower() in existing_files_all
                or filename in used_filenames[cat_dir]
            ):
                filename = f"{base_slug}_{counter}.md"
                counter += 1

            used_filenames[cat_dir].add(filename)

            # Manba va Maqsad
            source = (
                data.get("url")
                or fm.get("url")
                or data.get("shortcode")
                or fm.get("shortcode")
                or "Arxiv"
            )
            target = (
                it.get("note_uz")
                or data.get("title_en")
                or fm.get("title", "Prompt vazifasi")
            )

            prompt_record = {
                "filename": filename,
                "category_dir": cat_dir,
                "category_name": cat_name,
                "title": name,
                "target": target,
                "source": source,
                "unverified": is_unverified,
                "content": content_clean,
            }

            planned_files.append(prompt_record)
            category_counts[cat_dir] = category_counts.get(cat_dir, 0) + 1

    # Natijalarni ko'rsatish
    mode_str = "QURUQ REJIM (fayllar yozilmaydi)" if args.quruq else "HAQIQIY REJIM (fayllar yoziladi)"
    print("=" * 65)
    print(f"PROMT AJRATISH VA TOIFALASH HISOBOTI — {mode_str}")
    print("=" * 65)
    print(f"Arxiv papkasi     : {arxiv_dir}")
    print(f"Chiqish papkasi   : {chiqish_dir}")
    print(f"Mavjud fayllar    : {len(existing_files_all)} ta (himoyalangan)")
    print("-" * 65)
    print("STATISTIKA:")
    print(f"  • Jami topilgan bloklar         : {total_blocks}")
    print(f"  • Shulardan 'prompt' turi        : {total_prompt_blocks}")
    print(f"  • 'usable: false' tufayli o'tildi: {usable_false_skipped}")
    print(f"  • Takror promtlar tashlandi      : {duplicates_skipped} (matn xeshi bo'yicha)")
    print(f"  • Matnsiz (bo'sh) o'tkazildi     : {empty_content_skipped}")
    print(f"  • Yoziladigan haqiqiy promtlar   : {len(planned_files)}")
    print("-" * 65)
    print("TOIFALAR BO'YICHA TAQSIMOT:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        status = "yangi toifa" if cat in new_categories else "mavjud toifa"
        print(f"  • {cat:24s}: {count:2d} ta fayl  ({status})")

    if new_categories:
        print(f"\nYangi paydo bo'lgan toifalar: {', '.join(sorted(new_categories))}")

    # Kamida 3 ta namunani ko'rsatish
    print("\n" + "=" * 65)
    print("NAMUNA FAYLLAR (Qolip bo'yicha shakllantirilgan matn):")
    print("=" * 65)

    sample_indices = []
    # Turli xil holatlardan 3 ta namunani tanlaymiz:
    # 1. Unverified bo'lgan namuna
    for idx, p in enumerate(planned_files):
        if p["unverified"]:
            sample_indices.append(idx)
            break
    # 2. Dasturlash va AI namuna
    for idx, p in enumerate(planned_files):
        if p["category_dir"] == "dasturlash_va_ai" and idx not in sample_indices:
            sample_indices.append(idx)
            break
    # 3. Boshqa toifadan namuna
    for idx, p in enumerate(planned_files):
        if idx not in sample_indices:
            sample_indices.append(idx)
            break

    for i, idx in enumerate(sample_indices[:3], 1):
        sample = planned_files[idx]
        print(f"\n--- NAMUNA {i}: {sample['category_dir']}/{sample['filename']} ---")
        print(format_prompt_file(sample).strip())
        print("-" * 65)

    # Agar haqiqiy rejim bo'lsa (va --quruq bo'lmasa)
    if not args.quruq:
        print("\nFayllar yozilmoqda...")
        for p in planned_files:
            target_folder = chiqish_dir / p["category_dir"]
            target_folder.mkdir(parents=True, exist_ok=True)
            target_filepath = target_folder / p["filename"]
            with open(target_filepath, "w", encoding="utf-8") as out_fp:
                out_fp.write(format_prompt_file(p))
        print(f"Muvaffaqiyatli yakunlandi! {len(planned_files)} ta fayl yozildi.")
    else:
        print("\n[QURUQ REJIM] Hech qanday fayl yozilmadi. Nishon papka o'zgarmasdan saqlandi.")


if __name__ == "__main__":
    main()
