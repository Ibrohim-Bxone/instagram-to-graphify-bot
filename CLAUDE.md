# CLAUDE.md — Instagram → Graphify bot

## Loyihaning asosiy qoidasi

Runtime'da Claude ishtirok etmaydi. Pipeline'ga Claude API chaqiruvi qo'shmang —
butun qiymat shundaki, video qayta ishlash 0 token turadi. Sifat yetishmasa,
yechim lokal modelni yoki promptni almashtirish, Claude qo'shish emas.

## Arxitektura qarorlari (o'zgartirishdan oldin o'qing)

- **MD arxiv — haqiqat manbai, ChromaDB — derivativ indeks.** Har MD fayl oxirida
  to'liq yozuv JSON blok sifatida turadi. `src/reindex.py` shundan bazani noldan
  tiklaydi. Bu embedding modelini almashtirishni arzon qiladi — buzmang.
- **Deterministik ID:** `ig:<shortcode>:summary`, `ig:<shortcode>:item:<n>`.
  Har doim `upsert`, hech qachon `add` — bir reel ikki marta tashlansa ham
  yozuv bitta qoladi. 🗑 tugmasi ham shu ID'larga tayanadi.
- **🗑 tugmasi qasddan qaytarib bo'lmaydigan.** ChromaDB yozuvi VA MD arxiv fayli
  ikkalasi ham o'chadi (2026-08-11 foydalanuvchi tasdiqlagan qaror — bazani va
  diskni toza saqlash ustunroq deb topildi). `reindex.py` shu sababli o'chirilgan
  yozuvni tiklay olmaydi — bu kutilgan holat, xato emas.
- **`project="Promtlarim"`** — mavjud konvensiya (bazada allaqachon shu yorliq bor).
  Graphify izolyatsiyasi saqlanadi: boshqa loyihalarda ishlaganda Instagram
  kontenti retrieval'ga aralashmaydi, ochiq chaqirilganda topiladi.
- **`server.py` (Graphify) ga tegilmaydi.** Aniq loyiha nomi bilan qidirish
  allaqachon ishlaydi, patch kerak emas.
- **Bitta kolleksiya = bitta embedder.** `kb.py` Graphify'ning
  `build_chroma_embedding_function()` funksiyasini qayta ishlatadi. Boshqa
  embedder ishlatmoqchi bo'lsangiz, butun bazani qayta indekslash kerak.

## Til qoidasi

`all-MiniLM-L6-v2` faqat inglizchani tushunadi. Shuning uchun:

- `title_en`, `tags_en`, `name_en` — **inglizcha** (qidiruv langari)
- `summary_uz`, `note_uz`, `apply_suggestions_uz` — **o'zbekcha** (o'qish uchun)
- Promptlar — **asl tilida, so'zma-so'z**. Tarjima qilingan prompt qiymatsiz.

## Ekstraksiya qoidalari

- Ollama'ning `format` sxemasisiz chaqirmang — 12B model JSON'ni buzadi.
- Har ajratilgan element `extract.verify_item()` orqali transkriptga solishtiriladi.
  Tasdiqlanmagani o'chirilmaydi, `verified=False` bilan bayroqlanadi.
- `apply_suggestions_uz` — faqat tavsiya. Bot hech narsani avtomatik qo'llamaydi.

## Manba turlari (`source_type`)

| Qiymat | Kelib chiqishi | Bosqichlar |
|---|---|---|
| `url` | Instagram link | yt-dlp → whisper → [vision] → extract |
| `file` | Telegramga tashlangan video | whisper → [vision] → extract |
| `photo` | Forward post, rasm(lar) (albom ham) | vision → extract, whisper yo'q |
| `text` | Forward post, faqat matn | extract, whisper ham vision ham yo'q |

`pipeline.process()` shu ikki guruhga (`_process_video` / `_process_photo_or_text`)
tarmoqlanadi. Albom rasmlari `bot.py`dagi `_albums` bufer orqali
`ALBUM_DEBOUNCE_SEC` (1.5s) kutib bitta postga yig'iladi — Telegram har rasmni
alohida update qilib yuboradi, "oxirgisi shu" degan belgi bermaydi.

## VRAM

RTX 4060, 8GB. whisper large-v3 (~3GB) va gemma4:12b (~7.6GB) birga sig'maydi.
`transcribe.unload()` Ollama chaqirilishidan oldin majburiy. Worker ketma-ket
ishlaydi — parallel qilmang.

## Sozlashda

Prompt yoki model o'zgartirsangiz, `scripts/batch.py` bilan bir xil test
to'plamida o'lchang: vaqt, `items` soni, `verified` ulushi, `usable` ulushi.
Telegram orqali bittalab sinash — vaqt isrofi.
