#!/usr/bin/env python3
"""Instagram bot bosqichlarining vaqt sarfi bo'yicha hisobot skripti.

data/olchov.jsonl faylidan o'lchovlarni o'qib, har bir bosqich bo'yicha
o'rtacha, mediana, eng uzun vaqt va jami vaqtdagi ulushini (%) chiqaradi.
"""

import json
import statistics
import sys
from pathlib import Path

# Windows konsolida o'zbekcha / UTF-8 belgilarni to'g'ri chiqarish
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BOSQICHLAR = [
    ("yuklab_olish", "1. Yuklab olish"),
    ("audio_ajratish", "2. Audio ajratish"),
    ("transkripsiya", "3. Transkripsiya"),
    ("kadr_tahlili", "4. Kadr tahlili"),
    ("ajratish", "5. Ajratish (LLM)"),
    ("dublikat_va_indekslash", "6. Dublikat va indeks"),
    ("arxiv_va_telegram", "7. Arxiv va Telegram"),
]


def load_records(filepath: Path) -> list:
    if not filepath.exists():
        return []
    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def chop_etish(records: list, fayl_nomi: str = "data/olchov.jsonl") -> None:
    if not records:
        print(f"O'lchov ma'lumotlari topilmadi ({fayl_nomi} bo'sh yoki mavjud emas).")
        return

    jami_soni = len(records)
    xatolar = [r for r in records if r.get("xato")]
    muvaffaqiyatli_soni = jami_soni - len(xatolar)

    # Manba turlari statistikasi
    manba_hisobi = {}
    for r in records:
        mt = r.get("manba_turi") or r.get("manba turi") or "noma'lum"
        manba_hisobi[mt] = manba_hisobi.get(mt, 0) + 1
    manba_matn = ", ".join(f"{k}: {v}" for k, v in sorted(manba_hisobi.items()))

    # LLM backend statistikasi
    backend_hisobi = {}
    for r in records:
        b = r.get("llm_backend", "noma'lum")
        backend_hisobi[b] = backend_hisobi.get(b, 0) + 1
    backend_matn = ", ".join(f"{k}: {v}" for k, v in sorted(backend_hisobi.items()))

    # Har bir bosqich bo'yicha qiymatlarni yig'ish
    bosqich_qiymatlari = {k: [] for k, _ in BOSQICHLAR}
    jami_qiymatlari = []

    for r in records:
        for k, _ in BOSQICHLAR:
            if k in r and isinstance(r[k], (int, float)):
                bosqich_qiymatlari[k].append(float(r[k]))
        if "jami" in r and isinstance(r["jami"], (int, float)):
            jami_qiymatlari.append(float(r["jami"]))

    # O'rtacha, mediana va eng uzun qiymatlar
    ortachalar = {}
    medianalar = {}
    maxlar = {}

    for k, _ in BOSQICHLAR:
        vals = bosqich_qiymatlari[k]
        if vals:
            ortachalar[k] = statistics.mean(vals)
            medianalar[k] = statistics.median(vals)
            maxlar[k] = max(vals)
        else:
            ortachalar[k] = 0.0
            medianalar[k] = 0.0
            maxlar[k] = 0.0

    jami_ortacha = sum(ortachalar.values())
    jami_mediana = statistics.median(jami_qiymatlari) if jami_qiymatlari else 0.0
    jami_max = max(jami_qiymatlari) if jami_qiymatlari else 0.0

    # Jadval chiqarish
    print("=" * 84)
    print("                    INSTAGRAM BOT BOSQICHLAR VAQTI HISOBOTI")
    print("=" * 84)
    print(f"Fayl: {fayl_nomi}")
    print(f"Jami yozuvlar: {jami_soni} ta | Muvaffaqiyatli: {muvaffaqiyatli_soni} ta | Xatolik bilan: {len(xatolar)} ta")
    if manba_matn:
        print(f"Manba turlari: {manba_matn}")
    if backend_matn:
        print(f"LLM backend:   {backend_matn}")
    print("-" * 84)
    print(f"{'Bosqich':<26} | {'O`rtacha (s)':>12} | {'Mediana (s)':>11} | {'Eng uzun (s)':>12} | {'Ulushi (%)':>10}")
    print("-" * 27 + "+" + "-" * 14 + "+" + "-" * 13 + "+" + "-" * 14 + "+" + "-" * 12)

    for k, nom in BOSQICHLAR:
        avg_val = ortachalar[k]
        med_val = medianalar[k]
        max_val = maxlar[k]
        ulush = (avg_val / jami_ortacha * 100) if jami_ortacha > 0 else 0.0

        print(f"{nom:<26} | {avg_val:>11.2f}s | {med_val:>10.2f}s | {max_val:>11.2f}s | {ulush:>9.1f}%")

    print("-" * 27 + "+" + "-" * 14 + "+" + "-" * 13 + "+" + "-" * 14 + "+" + "-" * 12)
    print(f"{'JAMI':<26} | {jami_ortacha:>11.2f}s | {jami_mediana:>10.2f}s | {jami_max:>11.2f}s | {'100.0%':>10}")
    print("=" * 84)

    if xatolar:
        print("\nXatoliklar qayd etilgan bosqichlar:")
        xato_hisobi = {}
        for x in xatolar:
            st = x.get("xato") or "nomalum"
            xato_hisobi[st] = xato_hisobi.get(st, 0) + 1
        for st, soni in sorted(xato_hisobi.items()):
            print(f"  - {st}: {soni} ta")


def main():
    root = Path(__file__).resolve().parent
    default_path = root / "data" / "olchov.jsonl"
    fayl = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    records = load_records(fayl)
    chop_etish(records, str(fayl))


if __name__ == "__main__":
    main()
