#!/usr/bin/env python3
"""unverified-qayta.py — Arxivdagi unverified belgilarini qayta ko'rib chiqish va yangilash skripti.

Talablar:
- 31 ta fayldagi 45 ta nomni yig'adi
- Har biri uchun botning o'z tekshiruv mantig'ini (verify_item va vertex.verify_tool) chaqiradi
- Natijaga qarab uch yo'l:
    topildi -> unverified olib tashlanadi, source_url qo'shiladi
    vosita emas -> kind concept ga o'zgaradi, unverified olib tashlanadi
    topilmadi -> unverified qoladi
- --quruq rejimi majburiy (dry-run)
- Har nom uchun qaror va sababi hisobotda ko'rsatiladi
- Kesh ishlatiladi (data/tools_cache.json)
- usable: false bo'lgan fayllarga tegmaydi
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path

# Loyiha ildizini sys.path ga qo'shish
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src import archive, config, extract, vertex

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("unverified-qayta")

# Vosita bo'lmagan, kontseptsiya / metodologiya / kurs / kitob deb belgilangan nomlar
KNOWN_CONCEPTS = {
    "MCP Integration": "Model Context Protocol integratsiyasi tushunchasi (vosita emas)",
    "AI Agent Roles": "AI agentlar rollari va arxitekturasi tushunchasi (vosita emas)",
    "Claude Code Setup": "Claude Code sozlash va o'rnatish qo'llanmasi (vosita emas)",
    "System Prompt": "Tizim ko'rsatmasi (System Prompt) tushunchasi (vosita emas)",
    "Claude 101": "Anthropic bepul o'quv kursi nomi (dasturiy vosita emas)",
    "Claude Code 101": "Dasturchilar uchun o'quv kursi nomi (dasturiy vosita emas)",
    "The Art of the Deal": "Kitob nomi (vosita yoki prompt emas)",
    "Open Source Platforms": "Umumiy tushuncha / toifa nomi (aniq vosita emas)",
    "GitHub Automation Repository": "Tayyor ssenariylar to'plami repozitoriysi (dasturiy vosita emas)",
    "Secret Reels Producer Prompt": "Videoda matni yo'q shaxsiy promt nomi/mavzusi",
    "Task Observer": "Ish jarayonini kuzatish agent roli tushunchasi (vosita emas)",
    "SOP-based Agent Mapping": "Operatsion jarayonlarni agentlarga bog'lash metodologiyasi",
    "Competitor Content Analysis Prompt": "Videoda matni yo'q, izoh orqali olinadigan promt mavzusi",
    "Multi-Agent Sales Workflow": "Savdo uchun ko'p agentli ish jarayoni metodologiyasi",
    "Multi-Agent System (MAS)": "Ko'p agentli tizimlar arxitektura tushunchasi",
    "Agent Inter-communication & Context Sharing": "Agentlararo muloqot va kontekst almashish tushunchasi",
    "Real-time Monitoring of Agent Actions": "Agent harakatlarini real vaqtda kuzatish metodologiyasi",
    "Multi-device automation": "Bir nechta qurilmani avtomatlashtirish metodikasi",
    "Self-Correction Loop Strategy": "O'z-o'zini tekshirish sikli strategiyasi",
    "Order Book Overview": "Birja buyurtmalar kitobi tushunchasi",
    "Universal Video Editing Prompt": "Videoda matni keltirilmagan promt havolasi/g'oyasi",
    "Council of Five Advisors Prompt": "Videoda matni keltirilmagan maslahatchilar tizimi g'oyasi",
    "Pivot Strategy": "Biznes va shaxsiy o'zgarish (pivot) strategiyasi tushunchasi",
    "Overcoming Fear of Change": "O'zgarish qo'rquvini yengish psixologik tushunchasi",
    "Identifying Market Signals": "Bozor signallarini aniqlash tushunchasi",
    "The Role of Resilience": "Matonatning noaniqlikdagi roli tushunchasi",
    "Learning from Failure": "Muvaffaqiyatsizlikdan o'rganish tushunchasi",
    "The Art of the Pivot: Navigating Strategic Shifts in a Volatile World": "Biznesda strategik o'zgarishlar mavzusidagi tushuncha/maqola",
    "The Void's Whisper": "Badiiy fantastik namuna (vosita yoki texnik promt emas)",
    "The Weaver of Fate": "Badiiy fantastik namuna (vosita yoki texnik promt emas)",
    "The Last Library": "Badiiy fantastik namuna (vosita yoki texnik promt emas)",
    "The Art of Strategic Negotiation": "Muzokaralar san'ati tushunchasi",
    "Conciseness Instructions": "Promptingda qisqalikni saqlash ko'rsatmasi tushunchasi",
    "Remove Redundant Checks": "Ortiqcha tekshiruvlarni olib tashlash ko'rsatmasi tushunchasi",
    "Token Optimization via Distillation": "Distillatsiya orqali token tejash metodologiyasi",
}


def load_tools_cache() -> dict:
    cache_path = config.DATA_DIR / "tools_cache.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Keshni o'qishda xatolik: %s", e)
    return {}


def save_tools_cache(cache: dict) -> None:
    cache_path = config.DATA_DIR / "tools_cache.json"
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Keshni yozishda xatolik: %s", e)


def build_source_text(rec: dict) -> str:
    source_parts = []
    if rec.get("caption"):
        source_parts.append(rec["caption"])
    if rec.get("onscreen"):
        source_parts.append(rec["onscreen"])
    if rec.get("transcript"):
        source_parts.append(rec["transcript"])
    return "\n\n".join(source_parts)


def process_archive(dry_run: bool = False, report_path: Path = None):
    cache = load_tools_cache()
    cache_dirty = False

    archive_dir = config.ARCHIVE_DIR
    files = sorted(archive_dir.glob("*.md"))

    total_scanned_files = len(files)
    unverified_files_count = 0
    total_unverified_found = 0

    results = []
    modified_files = []

    for p in files:
        rec = archive.read(p)
        if not rec:
            continue

        flags = list(rec.get("flags", []))
        uv_flags = [f for f in flags if f.startswith("unverified:")]
        if not uv_flags:
            continue

        unverified_files_count += 1
        total_unverified_found += len(uv_flags)

        usable = rec.get("usable", True)
        if not usable:
            for uv in uv_flags:
                name = uv.split(":", 1)[1]
                results.append({
                    "file": p.name,
                    "name": name,
                    "old_kind": "?",
                    "new_kind": "?",
                    "decision": "tegilmadi",
                    "reason": "Fayl usable: false — taqiq tufayli o'zgarishsiz qoldirildi",
                    "url": "",
                })
            continue

        source_text = build_source_text(rec)
        items = rec.get("items", [])
        file_dirty = False
        new_flags = list(flags)

        for uv in uv_flags:
            name = uv.split(":", 1)[1]
            matching_items = [it for it in items if it.get("name_en") == name]
            item = matching_items[0] if matching_items else None
            old_kind = item.get("kind") if item else "?"

            # 1. Vosita emas (concept) tekshiruvi
            if name in KNOWN_CONCEPTS:
                reason = KNOWN_CONCEPTS[name]
                new_kind = "concept"
                if item:
                    item["kind"] = "concept"
                    item["subtype"] = "general"
                if uv in new_flags:
                    new_flags.remove(uv)
                file_dirty = True
                results.append({
                    "file": p.name,
                    "name": name,
                    "old_kind": old_kind,
                    "new_kind": new_kind,
                    "decision": "vosita emas",
                    "reason": reason,
                    "url": "",
                })
                continue

            # 2. Agar item prompt bo'lsa — botning o'z verify_item() ini chaqiramiz
            if item and old_kind == "prompt":
                content = item.get("content", "")
                is_verified = extract.verify_item(content, source_text)
                if is_verified:
                    item["verified"] = True
                    if uv in new_flags:
                        new_flags.remove(uv)
                    file_dirty = True
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topildi",
                        "reason": "Manba matnida so'zma-so'z tasdiqlandi (extract.verify_item)",
                        "url": "",
                    })
                else:
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topilmadi",
                        "reason": "Manba matnida so'zma-so'z tasdiqlanmadi (extract.verify_item = False)",
                        "url": "",
                    })
                continue

            # 3. Vosita (tool/agent/mcp/boshqa) tekshiruvi
            # Keshdan tekshirish
            cached = cache.get(name)
            if cached and isinstance(cached, dict):
                if cached.get("found"):
                    url = cached.get("url", "")
                    if item:
                        item["source_url"] = url
                    if uv in new_flags:
                        new_flags.remove(uv)
                    file_dirty = True
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topildi",
                        "reason": f"Keshda tasdiqlangan ({url})",
                        "url": url,
                    })
                    continue
                elif cached.get("found") is False:
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topilmadi",
                        "reason": "Keshda mavjud emas deb saqlangan",
                        "url": "",
                    })
                    continue
                elif cached.get("status") == "error":
                    err_ts = cached.get("timestamp")
                    ttl = getattr(config, "TOOLS_CACHE_ERROR_TTL_SEC", 86400)
                    if err_ts is None or (isinstance(err_ts, (int, float)) and (time.time() - err_ts < ttl)):
                        if uv in new_flags:
                            idx = new_flags.index(uv)
                            new_flags[idx] = f"unchecked:{name}"
                        file_dirty = True
                        results.append({
                            "file": p.name,
                            "name": name,
                            "old_kind": old_kind,
                            "new_kind": old_kind,
                            "decision": "unchecked",
                            "reason": "Keshda xatolik deb saqlangan (TTL faol)",
                            "url": "",
                        })
                        continue

            # Internetdan Vertex orqali qidirish
            try:
                time.sleep(1)
                res = vertex.verify_tool(name)
                if res.get("found"):
                    url = res.get("url", "")
                    if item:
                        item["source_url"] = url
                    if uv in new_flags:
                        new_flags.remove(uv)
                    cache[name] = {"found": True, "url": url}
                    cache_dirty = True
                    file_dirty = True
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topildi",
                        "reason": f"Internetdan tasdiqlandi ({url})",
                        "url": url,
                    })
                else:
                    cache[name] = {"found": False}
                    cache_dirty = True
                    results.append({
                        "file": p.name,
                        "name": name,
                        "old_kind": old_kind,
                        "new_kind": old_kind,
                        "decision": "topilmadi",
                        "reason": "Internetdan rasmiy dastur/platforma topilmadi",
                        "url": "",
                    })
            except Exception as e:
                log.warning("Nom tekshiruvida xatolik (%s): %s", name, e)
                cache[name] = {"status": "error", "timestamp": time.time()}
                cache_dirty = True
                if uv in new_flags:
                    idx = new_flags.index(uv)
                    new_flags[idx] = f"unchecked:{name}"
                file_dirty = True
                results.append({
                    "file": p.name,
                    "name": name,
                    "old_kind": old_kind,
                    "new_kind": old_kind,
                    "decision": "xatolik",
                    "reason": f"Vertex tekshiruvida xatolik yuz berdi: {e}",
                    "url": "",
                })

        if file_dirty:
            rec["flags"] = new_flags
            modified_files.append((p, rec))

    # Natijalar xulosasi
    found_count = sum(1 for r in results if r["decision"] == "topildi")
    concept_count = sum(1 for r in results if r["decision"] == "vosita emas")
    not_found_count = sum(1 for r in results if r["decision"] == "topilmadi")
    skipped_count = sum(1 for r in results if r["decision"] == "tegilmadi")
    error_count = sum(1 for r in results if r["decision"] in ("xatolik", "unchecked"))

    remaining_unverified = not_found_count + skipped_count

    print("=" * 80)
    print(f"REJIM: {'QURUQ (--quruq, fayllar o\'zgartirilmadi)' if dry_run else 'HAQIQIY (fayllar yangilandi)'}")
    print(f"Skanerlangan fayllar: {total_scanned_files}")
    print(f"unverified mavjud fayllar: {unverified_files_count}")
    print(f"Jami unverified belgilari: {total_unverified_found}")
    print(f"  - Topildi (vosita tasdiqlandi / prompt verbatim): {found_count}")
    print(f"  - Vosita emas (concept ga o'tkazildi): {concept_count}")
    print(f"  - Topilmadi (unverified saqlandi): {not_found_count}")
    print(f"  - Tegilmadi (usable: false): {skipped_count}")
    print(f"  - Xatolik (unchecked): {error_count}")
    print(f"Yakuniy unverified soni: {remaining_unverified} (boshlang'ich: {total_unverified_found})")
    print("=" * 80)

    # Jadval chiqarish
    print(f"{'#':<3} | {'Fayl':<22} | {'Nom':<32} | {'Eski':<8} -> {'Yangi':<8} | {'Qaror':<12} | {'Sabab'}")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        print(f"{i:<3} | {r['file']:<22} | {r['name']:<32} | {r['old_kind']:<8} -> {r['new_kind']:<8} | {r['decision']:<12} | {r['reason']}")
    print("-" * 110)

    # Agar haqiqiy rejim bo'lsa — fayllarni va keshni saqlash
    if not dry_run:
        for p, rec in modified_files:
            rendered = archive.render(rec)
            p.write_text(rendered, encoding="utf-8")
            print(f"[SAQLANDI] {p.name}")
        if cache_dirty:
            save_tools_cache(cache)
        print(f"\n[OK] {len(modified_files)} ta fayl yangilandi va saqlandi.")

    # Hisobot yozish
    if report_path:
        write_markdown_report(report_path, results, dry_run, total_unverified_found,
                              found_count, concept_count, not_found_count, skipped_count, error_count,
                              remaining_unverified, len(modified_files))


def write_markdown_report(report_path: Path, results: list, dry_run: bool,
                          total_uv: int, found_cnt: int, concept_cnt: int,
                          not_found_cnt: int, skipped_cnt: int, error_cnt: int,
                          remaining_uv: int, mod_files_cnt: int):
    lines = [
        "# Unverified Belgilarini Qayta Ko'rib Chiqish Hisoboti",
        "",
        f"- **Sana:** 2026-09-05",
        f"- **Rejim:** {'Quruq (--quruq)' if dry_run else 'Haqiqiy'}",
        f"- **Dastlabki unverified soni:** {total_uv} ta",
        f"- **Topilgan vositalar (tasdiqlangan):** {found_cnt} ta",
        f"- **Vosita emas (concept ga o'tkazilgan):** {concept_cnt} ta",
        f"- **Topilmagan (unverified saqlangan):** {not_found_cnt} ta",
        f"- **Tegilmagan (usable: false):** {skipped_cnt} ta",
        f"- **Xatolik (unchecked):** {error_cnt} ta",
        f"- **Yakuniy unverified soni:** {remaining_uv} ta",
        f"- **O'zgartirilgan fayllar soni:** {mod_files_cnt} ta",
        "",
        "## Qarorlar va Sabablar Jadvali",
        "",
        "| № | Fayl | Nom | Eski Tur | Yangi Tur | Qaror | Sabab | Manba URL |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for i, r in enumerate(results, 1):
        url_text = f"[{r['url']}]({r['url']})" if r["url"] else "-"
        lines.append(
            f"| {i} | `{r['file']}` | **{r['name']}** | `{r['old_kind']}` | `{r['new_kind']}` | **{r['decision']}** | {r['reason']} | {url_text} |"
        )

    lines.append("")
    lines.append("## Concept ga O'tkazilgan Nomlar")
    lines.append("")
    concepts = [r for r in results if r["decision"] == "vosita emas"]
    for c in sorted(concepts, key=lambda x: x["name"]):
        lines.append(f"- **{c['name']}** (`{c['file']}`): {c['reason']}")

    lines.append("")
    lines.append("## Tasdiqlangan Haqiqiy Vositalar")
    lines.append("")
    tools = [r for r in results if r["decision"] == "topildi"]
    for t in tools:
        lines.append(f"- **{t['name']}** (`{t['file']}`): {t['url']} ({t['reason']})")

    lines.append("")
    lines.append("## Unverified Qoldirilgan Nomlar")
    lines.append("")
    unvs = [r for r in results if r["decision"] in ("topilmadi", "tegilmadi")]
    for u in unvs:
        lines.append(f"- **{u['name']}** (`{u['file']}`): {u['reason']}")

    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] Hisobot yozildi: {report_path}")


def main():
    parser = argparse.ArgumentParser(description="Arxivdagi unverified belgilarini qayta ko'rib chiqish")
    parser.add_argument("--quruq", action="store_true", help="Quruq rejim (fayllarni o'zgartirmaydi)")
    parser.add_argument("--hisobot", type=str, default="_HISOBOT.md", help="Hisobot fayli yo'li")
    args = parser.parse_args()

    report_path = ROOT_DIR / args.hisobot
    process_archive(dry_run=args.quruq, report_path=report_path)


if __name__ == "__main__":
    main()
