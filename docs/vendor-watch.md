# Vendor Update Watcher (OpenAI / Anthropic / Google)

## 1. Tizim nima qiladi?
Vendor Update Watcher — OpenAI, Anthropic va Google ekotizimlarining rasmiy yangilanishlar oqimi (Atom va RSS feed'lari)ni doimiy kuzatib boruvchi avtomatlashtirilgan tizim.

Asosiy imkoniyatlari:
- **Tezkor va xolis saralash (Deterministic Triage):** LLM chaqiruvidan oldin SemVer va kalit so'zlar orqali reliz turini (`new_model`, `breaking`, `security`, `deprecation`, `pricing`, `feature`, `sdk_patch`, `other`) va uning muhimligini (`high` vs `digest`) aniqlaydi.
- **Dublikatlarni klasterlash:** 24 soat ichida turli manbalarda chiqqan bir xil relizlarni bitta xabarga birlashtiradi.
- **LLM tahlili va O'zbekcha hisobot:** Vertex AI Gemini orqali relizning foydalanuvchi stekiga (`profile.md`) aniq foydasini, qanday ishlatilishini va cheklovlarini ajratib, o'zbek tilida tayyorlaydi. LLM xato bersa ham "raw mode" orqali yetkazib berish uzilmaydi.
- **Telegram yetkazish va Outbox:** Tranzaksion outbox va `processing_jobs` orqali xabarlarni xavfsiz saqlaydi. Yuqori muhimlikdagi xabarlar darhol (kuniga 5 tagacha soft-cap, oshsa har biri to'liq qamrab olingan holda bo'lib yuboriladi), SDK patch'lari esa kunlik dayjest ko'rinishida yuboriladi.
- **Kunlik dayjest avtomatik flush:** `last_digest_date` holati orqali har 6 soatda ishga tushuvchi oddiy `--once` tsikllarida ham kuniga bir marta avtomatik yuboriladi (alohida `--digest` bayrog'i talab etilmaydi).
- **Xavfsiz Dry-run semantikasi:** `--dry-run` bayrog'i persistent bazani (diskdagi SQLite faylini) o'zgartirmaydi, in-memory nusxada ishlaydi. Hatto eski (commit 2401697) bazalarda ham fayl strukturasi yoki sxemasi diskda o'zgartirilmaydi.
- **Avariyadan tiklanish (Crash Recovery):** Tizim to'xtab qayta ishga tushganda navbatdagi hodisaning eng so'nggi reviziyasini oladi, maksimal xavfsizlik darajasini (severity) saqlab qoladi va oldin yuborilgan qabul qiluvchilarga qayta yubormaydi.
- **Klaster a'zolari va Dayjest xotirasi:** Birlashgan va dayjestga tushgan barcha manba hodisalari alohida (`event_notifications`) qayd etiladi, bu keyingi tsikllarda takroriy xabarnomalar chiqishini to'liq oldini oladi.
- **Pre-reliz shovqinidan tozalash:** `-alpha`, `-beta`, `-rc` versiyalar reliz sarlavhasi yoki tegi bo'yicha aniqlanadi (tana matnidagi ma'lumotlar adashtirmaydi) va dayjestdan ham, darhol xabarlardan ham chiqarib tashlanadi (agar xavfsizlik bo'lmasa).
- **Qabul qiluvchilar bo'yicha mustaqil kuzatuv:** Bir necha chat_id bo'lganda, ularning har biriga yetkazish alohida kuzatiladi va xato yuz berganda faqat hozirgi konfiguratsiyada mavjud bo'lgan kutilayotgan chatlarga qayta urinish qilinadi.

---

## 2. Rasmiy manbalar (Sources)
Hozirda quyidagi 1-darajali (tier-1) rasmiy oqimlar ulab qo'yilgan:
1. `anthropic-claude-code`: GitHub releases Atom (`anthropics/claude-code`)
2. `openai-codex`: GitHub releases Atom (`openai/codex`)
3. `google-gemini-cli`: GitHub releases Atom (`google-gemini/gemini-cli`)
4. `google-python-genai`: GitHub releases Atom (`googleapis/python-genai`)
5. `anthropic-sdk-python`: GitHub releases Atom (`anthropics/anthropic-sdk-python`)
6. `openai-python`: GitHub releases Atom (`openai/openai-python`)
7. `openai-news`: OpenAI News RSS (`https://openai.com/news/rss.xml`)
8. `google-vertex-ai-release-notes`: Google Cloud Vertex AI Release Notes Atom (`https://cloud.google.com/feeds/vertex-ai-release-notes.xml`)
9. `google-developers-blog`: Google Developers Blog Atom (`https://developers.googleblog.com/feeds/posts/default`)
10. `google-blog-gemini`: Google Gemini Blog RSS (`https://blog.google/products/gemini/rss/`)
11. `google-deepmind-blog`: Google DeepMind Blog RSS (`https://blog.google/technology/google-deepmind/rss/`)

---

## 3. Yangi manba qo'shish tartibi
Yangi manba qo'shish uchun kodni o'zgartirish talab qilinmaydi. Faqat `src/vendor_watch/sources.json` fayliga yangi yozuv qo'shiladi:

```json
{
  "id": "yangi-manba-id",
  "vendor": "Anthropic",
  "product": "Claude Agent SDK",
  "url": "https://github.com/anthropics/agent-sdk/releases.atom",
  "kind": "atom"
}
```

Format talablari:
- `id`: manbani aniqlovchi takrorlanmas matn (masalan, `vendor-product`).
- `vendor`: `Anthropic`, `OpenAI` yoki `Google`.
- `product`: mahsulot nomi.
- `url`: to'g'ridan-to'g'ri Atom yoki RSS XML oqimining manzili.
- `kind`: `atom` yoki `rss`.

---

## 4. Windows Task Scheduler (`schtasks`) bilan har 6 soatda ishga tushirish

Har 6 soatda skriptni orqa fonda avtomatik yurgizish va agar kompyuter o'chiq bo'lgan vaqtda jadval o'tib ketgan bo'lsa, kompyuter yoqilganda darhol ishga tushirish ("run when missed") uchun buyruq:

### Standard schtasks buyrug'i:
```cmd
schtasks /create /tn "VendorWatch_6Hours" /tr "\"D:\claude projects\graphify-ekotizim\_wt-vendor-watch\scripts\vendor_watch.bat\"" /sc hourly /mo 6 /f
```

### "Run when missed" (StartWhenAvailable) xususiyatini faollashtirish:
Windows Task Scheduler'da o'tkazib yuborilgan rejalashtirilgan vazifani kompyuter yoqilishi bilan bajarish uchun PowerShell orqali quyidagi sozlama qo'llaniladi:

```powershell
$action = New-ScheduledTaskAction -Execute "D:\claude projects\graphify-ekotizim\_wt-vendor-watch\scripts\vendor_watch.bat"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 6)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "VendorWatch_6Hours" -Action $action -Trigger $trigger -Settings $settings -Force
```

> **Eslatma:** Ushbu buyruqlar faqat ma'lumot va qo'llanma uchun keltirilgan, ruxsatsiz tizimga avtomatik ro'yxatdan o'tkazilmaydi.

---

## 5. Tier-3 Akkaunt Problari (Account & Tool Probes)

Vendor Watch nafaqat vendorlarning rasmiy e'lonlarini (feed), balki foydalanuvchining shaxsiy hisoblari va lokal asboblarida nima amalda mavjud yoki o'zgarganini aniqlash uchun **Tier-3 Account Probes** mexanizmini o'z ichiga oladi.

### 5.1. Statuslar semantikasi
Tizim uchta holatni aniq farqlaydi:
1. **Announced (E'lon qilingan):** Vendor Atom/RSS feed'larida ko'ringan yangilik.
2. **Listed (Ro'yxatga olingan):** `agy models` CLI buyrug'i orqali qaytarilgan modellar ro'yxati.
3. **Tested on this account (Akkauntda sinovdan o'tgan):** Shaxsiy Google Cloud Vertex AI loyihasida (`project-92b77c1d-c511-47ce-965`, `global`) `count_tokens` orqali tekshirilgan va HTTP 200 qaytargan modellar.

### 5.2. Asosiy 4 ta prob (`probes.json` orqali boshqariladi):
- `agy_models`: Antigravity (`agy models`) buyrug'i orqali olingan model ID'lar to'plami diffi. Yangi model qo'shilsa yoki o'chsa — darhol `high` prioriteti bilan xabar beriladi.
- `agy_changelog`: `agy changelog` buyrug'idan yangi versiya yozuvlarini va bullet punktlarini ajratib oladi (`digest` prioriteti).
- `cli_versions`: Mahalliy kompyuterda o'rnatilgan `codex`, `claude` va `agy` CLI versiyalarini GitHub'dagi eng so'nggi Atom relizlari bilan solishtiradi. Eskirgan asboblar haqida dayjest orqali xabar beradi.
- `vertex_models`: Nomzod modellar ro'yxatini (boshlang'ich ro'yxat + yangiliklar oqimidan triage topgan modellar) Vertex AI'da bepul `count_tokens` so'rovi bilan tekshiradi. HTTP 200 — mavjud, HTTP 404 — mavjud emas.

### 5.3. Xavfsiz xatolik semantikasi
- **Bazaviy holat (Baseline):** Prob birinchi marta muvaffaqiyatli ishlaganda natija SQLite'dagi `probe_snapshots` jadvaliga bazaviy holat sifatida yoziladi; soxta qo'shildi/o'chirildi xabarlari chiqarilmaydi.
- **Prob xatosi o'chirish emas (Probe failure ≠ removal):** Tarmoq uzilishi, timeout yoki avtorizatsiya (401/403/500) xatosi hech qachon model "o'chirildi" degan xulosaga olib kelmaydi. Xatolik qayd etiladi va keyingi tsiklda qayta uriniladi. Faqat toza HTTP 404 javobigina model yo'qolganini tasdiqlaydi.
- **404 → 200 o'tishi:** Oldin 404 bergan model 200 bera boshlasa, foydalanuvchi hisobida model faollashgani aniqlanadi va darhol `high` prioriteti bilan xabar beriladi.

### 5.4. O'zbekcha xabar formati va foydalanuvchi steki
Har bir akkaunt o'zgarishi `renderer.py` orqali foydalanuvchi profili (`profile.md`) kontekstida yoritiladi:
- Sarlavha: `🔔 Sizning akkauntingizda: <model> paydo bo'ldi / yo'qoldi`
- `📌 Nima?`: Amalda nima yuz bergani (masalan, Vertex AI yoki agy ro'yxatida aniqlangani).
- `🎯 Sizga foydasi:`: Foydalanuvchi steki (`profile.md`) uchun ma'nosi (masalan, `argon-ish.py` klipboard ko'prigi, `agy-run.py` konveyeri yoki `Antigravity Switcher` sozlamalariga ta'siri).
- `💻 Qanday ishlatish:`: Modelni sinash yoki yangilash uchun aniq buyruq.

### 5.5. CLI boshqaruvi
- `python -m src.vendor_watch --probes-only`: Faqat hisob va asbob problarini yurgizish (tashqi feed'larni yuklamasdan).
- `python -m src.vendor_watch --once`: Odatiy monitoring tsikli (tashqi feed'lar va prob tekshiruvlarini birgalikda o'z ichiga oladi).
- `python -m src.vendor_watch --fixtures`: Problar uchun tayyor test ma'lumotlaridan (`CANNED_PROBE_OUTPUTS`) foydalanadi (tashqi tarmoq so'rovlarisiz).

