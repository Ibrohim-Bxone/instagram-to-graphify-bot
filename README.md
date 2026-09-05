# Instagram/Telegram → Knowledge Base Bot

**[English](#english) | [O'zbekcha](#ozbekcha)**

---

<a id="english"></a>
## English

Send an Instagram reel link, a YouTube link, a video file, or a forwarded
Telegram post (photo/video/text) to this bot — it reads it, extracts the
substance (prompts, skills, useful tools), and saves it into a searchable
knowledge base.

**No external AI API (Claude, OpenAI, etc.) is used at runtime — zero token
cost.** Everything runs locally on your own machine: transcription via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), extraction and
frame reading via [Ollama](https://ollama.com) with a local LLM (default:
`gemma4:12b`).

### What the bot does

| You send | The bot does |
|---|---|
| Instagram reel/post link | Downloads it (video or carousel photos), transcribes, extracts the substance |
| YouTube link | Same |
| Video file (sent directly) | Transcribes, reads frames if needed |
| Photo(s) + text (forwarded post, albums too) | Reads on-screen text, analyzes the caption |
| Text only (link inside a forwarded post) | Saves the text and any links in it |

For every item, the bot produces:
- A short summary
- Extracted prompts/skills/tools (in their **original language, verbatim** —
  never translated or rewritten)
- A suggestion for how this could apply to your own projects (**a suggestion
  only — nothing is applied automatically**)
- A heads-up if something similar already exists in the knowledge base (does
  not block saving, just flags it)

Results are written to two places:
1. **Markdown archive** (`data/archive/<code>.md`) — the source of truth, the
   full transcript and analysis live here, never auto-deleted (only the 🗑
   button removes it).
2. **ChromaDB** (a semantic-searchable vector store) — an index that can always
   be rebuilt from the archive.

### Why it's built this way

- **Zero token/money cost at runtime.** The whole point is that processing a
  video/post costs nothing — all AI work runs on local models on your machine.
  If quality is lacking, the fix is a better local model or prompt, not adding
  an external API.
- **Markdown is the source of truth, the database is derived.** Every markdown
  file ends with the full record embedded as a JSON block. `src/reindex.py`
  rebuilds the database from these files from scratch — nothing is lost even
  if the database breaks or you switch embedding models.
- **Only suggests, never decides for you.** The "how this could apply to your
  projects" section is there to inform your decision — it's never applied
  automatically.
- **Everything gets checked, nothing is blindly trusted.** Every extracted
  "prompt" is verified against the actual transcript/frame text — if it
  doesn't match, it's flagged with ⚠️ rather than silently deleted (you decide).

### Requirements

- Python 3.11+ (tested on Windows)
- [Ollama](https://ollama.com), with a model pulled: `ollama pull gemma4:12b` (~7.6GB)
- A Telegram bot token (free, via [@BotFather](https://t.me/BotFather))
- An NVIDIA GPU is recommended (8GB+ VRAM) — works without one too, just slower on CPU
- `ffmpeg` does not need separate installation — bundled via `imageio-ffmpeg`

### Installation

```bash
git clone https://github.com/Ibrohim-Bxone/instagram-to-graphify-bot.git
cd instagram-to-graphify-bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
```

If you have an NVIDIA GPU, install these for GPU-accelerated `faster-whisper`
(optional, but noticeably faster):

```bash
.venv\Scripts\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Copy `.env.example` to `.env` and fill it in:

```bash
copy .env.example .env
```

#### Deploying with an AI coding assistant

Don't want to do the setup by hand? Paste this into Claude Code, Cursor, or any
AI coding assistant with terminal access, in the folder where you want the
project:

````text
Clone and deploy https://github.com/Ibrohim-Bxone/instagram-to-graphify-bot
on this machine. Steps:

1. Clone the repo, create a Python 3.11+ venv, install requirements.txt.
2. Check whether Ollama is installed (`ollama --version`). If not, tell me to
   install it from https://ollama.com and stop until I confirm it's done.
   Then run `ollama pull gemma4:12b` (~7.6GB download).
3. Detect whether an NVIDIA GPU is present. If yes, additionally install
   nvidia-cublas-cu12 and nvidia-cudnn-cu12 into the venv for faster
   transcription.
4. Copy .env.example to .env. Ask me for BOT_TOKEN (I'll get it from
   @BotFather on Telegram) and ALLOWED_USER_IDS (I'll get my numeric ID from
   @userinfobot on Telegram) and fill those two into .env — leave every other
   variable at its default.
5. Run `python -m scripts.doctor` and fix anything it reports as FAIL before
   proceeding — don't just tell me about it, actually resolve it (missing
   model, wrong path, etc.) and re-run doctor until everything passes.
6. Start the bot (`python -m src.bot`) and confirm in the logs that it's
   polling Telegram with no errors.
7. Tell me to open Telegram, message the bot /start, and send it one
   Instagram/YouTube link to confirm the full pipeline works end to end.

Ask me one question at a time when you need something from me (the bot token,
confirming Ollama is installed, etc.) — don't guess or skip a step.
````

This works because every step above matches an actual command in this
project — the AI doesn't need to guess anything.

### Configuration (`.env`)

**Only two variables are required to get started:**

| Variable | What it's for | Where to get it |
|---|---|---|
| `BOT_TOKEN` | Telegram bot API key | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ALLOWED_USER_IDS` | Only these Telegram ID(s) can use the bot (comma-separated, multiple allowed) | Message [@userinfobot](https://t.me/userinfobot) — it replies with your ID |

**Everything else is optional — sensible defaults work out of the box:**

| Variable | Default | What it does |
|---|---|---|
| `MODEL_EXTRACT` / `MODEL_VISION` | `gemma4:12b` | Ollama model for extraction and frame reading. Must support vision |
| `OLLAMA_NUM_CTX` | `8192` | Model context length. Don't raise this on 8GB VRAM cards — the model partially offloads to CPU and gets several times slower |
| `WHISPER_MODEL` | `large-v3` | Transcription quality/speed trade-off. Use `medium` or `small` for more speed |
| `VISION_MODE` | `auto` | `auto` — only reads frames when the transcript is thin (faster); `always`; `never` |
| `DUPLICATE_SIMILARITY_THRESHOLD` | `0.75` | Similarity above this score gets flagged as a likely duplicate. Empirically measured: unrelated topics score ~0.10-0.15, real duplicates score ~0.80 |
| `ARCHIVE_DIR` | `data/archive` (inside the repo) | Where the markdown archive is written |
| `GRAPHIFY_DIR` | *(empty — standalone mode)* | See below |
| `YTDLP_COOKIES_FROM_BROWSER` | *(empty)* | If Instagram requires login: `chrome`/`edge`/`firefox`. **Use a burner account** — automated downloading can get your main account limited |

#### Knowledge base: standalone mode vs. Graphify integration

The bot can run in two modes, and **works standalone with zero extra setup**:

- **Standalone mode (default, when `GRAPHIFY_DIR` is empty).** The bot creates
  its own ChromaDB (`data/kb_db`). Nothing else to install — clone, fill in
  `.env`, and run.
- **Graphify integration (optional).** If you already run
  [Graphify](https://github.com) (a separate, personal knowledge-base tool),
  point `GRAPHIFY_DIR` in `.env` at that project's folder — everything this bot
  saves lands in the same database as your other projects.

`scripts/doctor.py` reports which mode is active and the exact database
location (see below).

### Running it

```bash
.venv\Scripts\python -m src.bot
```

Or double-click `run_bot.bat` on Windows.

Before running, check that everything is configured correctly:

```bash
.venv\Scripts\python -m scripts.doctor
```

This checks ffmpeg, whisper (GPU/CPU), Ollama models, the knowledge-base
backend, the archive folder, and Telegram config — and tells you exactly
what's missing.

#### Auto-start on boot (Windows)

To have the bot start automatically every time you log in, and restart itself
if it ever crashes:

```powershell
$proj = (Get-Location).Path
$startup = [Environment]::GetFolderPath("Startup")
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut((Join-Path $startup "InstagramGraphifyBot.lnk"))
$shortcut.TargetPath = "$proj\.venv\Scripts\pythonw.exe"
$shortcut.Arguments = "-m scripts.watchdog"
$shortcut.WorkingDirectory = $proj
$shortcut.WindowStyle = 7
$shortcut.Save()
```

This drops a shortcut into your Startup folder that runs `scripts/watchdog.py`
— a small supervisor that launches the bot windowlessly and relaunches it
immediately if it ever exits, logging each restart to `data/logs/watchdog.log`.
(Windows Task Scheduler is the "proper" way to do this, but it's often locked
down by corporate/domain group policy — a Startup-folder shortcut needs no
elevated permissions and works everywhere.)

Messages sent while the bot was offline are not lost: Telegram holds them
server-side until the bot reconnects and processes them in order, so nothing
needs to be resent.

### Usage (Telegram commands)

Start with `/start`. Then:

| You send | Result |
|---|---|
| Video link (Instagram, YouTube, TikTok, X, Reddit, Facebook, Vimeo, LinkedIn, …) | Queued, you get a message when it's done |
| A bare link to any article (habr, a blog, docs) | The page text is fetched and extracted — no video involved |
| Video file (≤20MB, or 2GB with a self-hosted Bot API server) | Same |
| Photo(s) (single or album) + caption | Same |
| Forwarded text (with a link) | Same |
| `/video <link>` | Forces the video pipeline for a host not in the recognised list |
| `/status` | Queue status and recent jobs |
| `/users` | **Admin only.** Panel with a ➕ button to grant access by Telegram id, and ❌ buttons to revoke |
| `/install <shortcode>` | **Admin only, off by default.** Offers to install what an entry mentions — see below |
| `/search <query>` | Searches your saved knowledge (e.g. `/search prompt chaining`) |
| `/search all: <query>` | Searches **across all projects** in the knowledge base (useful in Graphify mode) |
| 🗑 button | **Permanently deletes** the entry from the database AND the archive file — cannot be undone |
| 🔄 button | Re-extracts from the archived transcript (doesn't re-download — fast) |

You can send many items at once — the bot processes them one at a time (GPU
memory is limited, no parallelism), and messages you as each one finishes.

#### Access tiers

| Tier | Where it lives | Can ingest | Can add users | Can install |
|---|---|---|---|---|
| Admin | `ADMIN_USER_IDS` in `.env` | yes | yes | yes |
| `.env` user | `ALLOWED_USER_IDS` | yes | no | no |
| Added user | ➕ button, stored in `data/members.json` | yes | no | no |

Admins are configured in `.env` only. A Telegram message can never promote its
own sender, so the ➕ button grants ingest access and nothing more. A user finds
their own id by sending `/start`.

#### `/install` — reading a link, then installing what it mentions

Off unless `INSTALL_ENABLED=true`, and admin-only even then. The text it works
from came off the internet, so a reel or article can name any package it likes.
The design assumes that:

- Four installers exist and no others: `git clone`, `npm install -g`,
  `pip install`, `ollama pull`. There is no code path for anything else.
- No command string is ever taken from the extracted text — only a **name**,
  which must match a pattern that excludes whitespace, shell metacharacters and
  leading dashes. It is then placed into a fixed argv this repo owns.
- `subprocess` runs with `shell=False`. No pipes, no `curl | bash`.
- Two taps: the first shows the exact command and target folder, the second runs
  it. The offer is held in memory, so a restart voids every pending button.
- Clones land in `TOOLS_DIR`/`SKILLS_DIR`, both inside `data/` by default.

It is still a channel that runs commands on your machine from a chat message.
Leave it off unless you want that.

### How it works (pipeline)

**Instagram/YouTube link or video file:**
```
yt-dlp or a direct file           (caption/description is captured too)
  → ffmpeg → faster-whisper       (VAD filter, automatic language detection)
  → [if transcript is thin or absent] scene-change frames → LLM vision (on-screen text)
  → LLM → classification + summary + prompt/skill extraction + apply suggestions
  → extracted items are verified against the source (verbatim-match check)
  → [checked against the knowledge base for near-duplicates]
  → data/archive/<code>.md   ← source of truth
  → ChromaDB                 ← searchable index
```

**Telegram post (photo/video/text, forwarded):**
```
Post
  → links extracted from the text (kept for reference)
  → photo(s) → LLM vision (on-screen text)
  → same classification/summary/extraction/verification pipeline
  → same markdown archive + ChromaDB
```

If a video has no audio track (e.g. a reposted reel), transcription is skipped
and only frames are analyzed — the pipeline doesn't fail on this.

### Development

```bash
# Benchmark against a fixed test set (much faster than testing one-by-one via Telegram)
.venv\Scripts\python -m scripts.batch tests/fixtures --dry-run

# Tune the extraction prompt only (no re-download)
.venv\Scripts\python -m scripts.batch tests/fixtures --extract-only

# Rebuild the database from the archive (needed after switching embedding models)
.venv\Scripts\python -m src.reindex --purge

# Unit tests
.venv\Scripts\python -m pytest tests -q
```

`scripts/batch.py` uses its own isolated job queue and database — it never
touches the live Telegram bot's queue.

### Security

- `.env` is **never committed** (in `.gitignore`) — your bot token and Telegram
  ID live there, keep them private.
- `data/` is also never committed — real transcripts, chat IDs, and queue data
  live there.
- If you fork/clone this repo, copy `.env.example` and fill in your own
  `BOT_TOKEN` and `ALLOWED_USER_IDS`.

### Limitations

- The Telegram Bot API caps file uploads at 20MB — for larger videos, send the
  link instead of the file.
- If Instagram/YouTube requires login, set `YTDLP_COOKIES_FROM_BROWSER` (with a
  burner account).
- On 8GB-VRAM cards, whisper and the LLM don't fully fit together — they run
  sequentially, whisper frees VRAM after each job.
- The default embedding model (`all-MiniLM-L6-v2`) only understands English
  well — that's why titles/tags are written in English (they're the search
  anchors), while explanatory text stays in the source language.

### License

MIT — see [LICENSE](LICENSE).

---

<a id="ozbekcha"></a>
## O'zbekcha

Instagram reel linkini, YouTube linkini, videoning o'zini, yoki Telegram post
(rasm/video/matn, forward qilingan) tashlaysiz — bot uni o'qiydi, mag'zini
ajratadi (promptlar, skilllar, foydali vositalar) va qidiriladigan bilim
bazasiga saqlaydi.

**Runtime'da hech qanday tashqi AI API (Claude, OpenAI va h.k.) ishlatilmaydi —
0 pul/token sarfi.** Hamma ish lokal kompyuteringizda ishlaydi: transkripsiya
uchun [faster-whisper](https://github.com/SYSTRAN/faster-whisper), mag'z
ajratish va kadr o'qish uchun [Ollama](https://ollama.com) orqali lokal LLM
(standart: `gemma4:12b`).

### Bot nima qiladi

| Siz yuborasiz | Bot nima qiladi |
|---|---|
| Instagram reel/post linki | Yuklab oladi (video yoki karusel rasmlari), transkript qiladi, mag'zini ajratadi |
| YouTube linki | Xuddi shunday |
| Video fayl (to'g'ridan-to'g'ri) | Transkript qiladi, kerak bo'lsa kadrlarni o'qiydi |
| Rasm(lar) + matn (forward post, albom ham) | Rasmlardagi matnni o'qiydi, captiondagi matnni tahlil qiladi |
| Faqat matn (link bilan forward post) | Matnni va undagi havolalarni saqlaydi |

Har bir yuborilgan narsadan bot quyidagilarni chiqarib oladi:
- Qisqa xulosa (o'zbek tilida)
- Ajratilgan promptlar/skilllar/vositalar (asl tilida, so'zma-so'z — tarjima qilinmaydi)
- "Bu loyihalaringizda qanday qo'llash mumkin" tavsiyasi (**faqat tavsiya — hech
  narsa avtomatik qo'llanilmaydi**)
- Agar shunga o'xshash narsa bazada allaqachon bo'lsa — ogohlantiradi (saqlashni
  bloklamaydi)

Natija ikki joyga yoziladi:
1. **Markdown arxiv** (`data/archive/<kod>.md`) — haqiqat manbai, to'liq transkript
   va tahlil shu yerda, hech qachon avtomatik o'chirilmaydi (faqat 🗑 tugmasi bilan).
2. **ChromaDB** (semantik qidiriladigan vektor baza) — arxivdan har doim qayta
   quriladigan indeks.

### Nega bunday qurilgan (arxitektura falsafasi)

- **0 token/pul runtime'da.** Video/post qayta ishlash hech narsa turmasin
  degan maqsadda — barcha AI ishi kompyuteringizdagi lokal modellar bilan
  bajariladi. Agar natija sifati yetishmasa, yechim — promptni yoki modelni
  almashtirish, tashqi API qo'shish emas.
- **Markdown — haqiqat manbai, baza — derivativ.** Har MD fayl oxirida to'liq
  yozuv JSON blok sifatida saqlanadi. `src/reindex.py` shundan bazani noldan
  tiklaydi — baza buzilsa yoki embedding modelini almashtirsangiz ham hech narsa
  yo'qolmaydi.
- **Faqat tavsiya beradi, hech narsani o'zi hal qilmaydi.** "Loyihalarda qanday
  qo'llash mumkin" degan bo'lim — sizga qaror qabul qilish uchun, avtomatik
  amalga oshirilmaydi.
- **Har narsa tekshiriladi, ko'r-ko'rona ishonilmaydi.** Ajratilgan har bir
  "prompt" transkript/kadr matniga solishtirilib tekshiriladi — mos kelmasa
  ⚠️ bilan belgilanadi, o'chirilmaydi (siz ko'rib qaror qilasiz).

### Talab qilinadigan narsalar

- Python 3.11+ (Windows uchun sinalgan)
- [Ollama](https://ollama.com), va model: `ollama pull gemma4:12b` (~7.6GB)
- Telegram bot token ([@BotFather](https://t.me/BotFather) orqali bepul olinadi)
- NVIDIA GPU tavsiya etiladi (8GB+ VRAM) — bo'lmasa ham ishlaydi, lekin
  sekinroq (CPU orqali)
- `ffmpeg` alohida o'rnatish shart emas — `imageio-ffmpeg` paketi ichida keladi

### O'rnatish

```bash
git clone https://github.com/Ibrohim-Bxone/instagram-to-graphify-bot.git
cd instagram-to-graphify-bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows
```

NVIDIA GPU'ingiz bo'lsa, `faster-whisper`ni GPU'da ishlatish uchun (ixtiyoriy,
lekin sezilarli tezlashtiradi):

```bash
.venv\Scripts\pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

`.env.example` faylini `.env` ga nusxalang va to'ldiring:

```bash
copy .env.example .env
```

#### AI yordamchi orqali deploy qilish

Qo'lda sozlashni xohlamasangiz — quyidagini Claude Code, Cursor yoki terminalga
kira oladigan istalgan AI yordamchiga, loyihani joylashtirmoqchi bo'lgan
papkada, to'g'ridan-to'g'ri nusxalab bering:

````text
https://github.com/Ibrohim-Bxone/instagram-to-graphify-bot ni shu kompyuterda
klonlab, ishga tushiring. Bosqichlar:

1. Repo'ni klonlang, Python 3.11+ venv yarating, requirements.txt'ni o'rnating.
2. Ollama o'rnatilganini tekshiring (`ollama --version`). O'rnatilmagan bo'lsa,
   https://ollama.com dan o'rnatishimni so'rang va men tasdiqlaguncha
   to'xtang. Keyin `ollama pull gemma4:12b` (~7.6GB) ni ishga tushiring.
3. NVIDIA GPU bor-yo'qligini aniqlang. Bo'lsa, transkripsiyani tezlashtirish
   uchun venv'ga qo'shimcha nvidia-cublas-cu12 va nvidia-cudnn-cu12'ni
   o'rnating.
4. .env.example'ni .env'ga nusxalang. Mendan BOT_TOKEN (Telegram'dagi
   @BotFather'dan olaman) va ALLOWED_USER_IDS (Telegram'dagi @userinfobot'dan
   raqamli ID'imni olaman) so'rang va shu ikkitasini .env'ga yozing — qolgan
   hamma narsa standart holida qolsin.
5. `python -m scripts.doctor` ni ishga tushiring va FAIL deb chiqqan hamma
   narsani tuzating — shunchaki aytib qo'ymang, haqiqatan hal qiling (model
   yetishmasa, yo'l noto'g'ri bo'lsa va h.k.) va hammasi o'tguncha qayta-qayta
   ishga tushiring.
6. Botni ishga tushiring (`python -m src.bot`) va log'da Telegram'ga xatosiz
   ulanganini tasdiqlang.
7. Menga Telegram'da botga /start yozib, keyin bitta Instagram/YouTube
   linkini tashlab, butun jarayon oxirigacha ishlashini tekshirishni ayting.

Sizga kerak bo'lgan narsani (bot token, Ollama o'rnatilganini tasdiqlash va
h.k.) bir vaqtda bittadan so'rang — hech narsani taxmin qilib o'tkazib
yubormang.
````

Bu ishlaydi, chunki yuqoridagi har bir qadam ushbu loyihadagi aniq buyruqqa mos
keladi — AI hech narsani taxmin qilishi shart emas.

### Sozlash (`.env`)

**Minimal ishga tushirish uchun faqat ikkitasi majburiy:**

| O'zgaruvchi | Nima uchun | Qayerdan olinadi |
|---|---|---|
| `BOT_TOKEN` | Telegram bot API kaliti | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ALLOWED_USER_IDS` | Faqat shu Telegram ID(lar) botdan foydalana oladi (vergul bilan, bir nechta bo'lishi mumkin) | [@userinfobot](https://t.me/userinfobot) ga yozing — ID'ingizni aytadi |

**Qolgan hammasi ixtiyoriy — standart qiymatlar bilan ham to'liq ishlaydi:**

| O'zgaruvchi | Standart | Nima qiladi |
|---|---|---|
| `MODEL_EXTRACT` / `MODEL_VISION` | `gemma4:12b` | Mag'z ajratish va kadr o'qish uchun Ollama modeli. Vision qobiliyati bor model bo'lishi shart |
| `OLLAMA_NUM_CTX` | `8192` | Model konteksti. 8GB VRAM'li kartalarda oshirmang — model qisman protsessorga tushib, bir necha barobar sekinlashadi |
| `WHISPER_MODEL` | `large-v3` | Transkripsiya sifati/tezlik nisbati. Tezroq kerak bo'lsa `medium` yoki `small` |
| `VISION_MODE` | `auto` | `auto` — transkript qisqa bo'lsagina kadrlarni o'qiydi (tezroq); `always` — har doim; `never` — hech qachon |
| `DUPLICATE_SIMILARITY_THRESHOLD` | `0.75` | Bu balldan yuqori o'xshashlik "ehtimol dublikat" deb belgilanadi. Haqiqiy o'lchov: mos kelmaydigan mavzular ~0.10-0.15, haqiqiy dublikatlar ~0.80 ball beradi |
| `ARCHIVE_DIR` | `data/archive` (repo ichida) | Markdown arxiv qayerga yoziladi |
| `GRAPHIFY_DIR` | *(bo'sh — mustaqil rejim)* | Quyida batafsil |
| `YTDLP_COOKIES_FROM_BROWSER` | *(bo'sh)* | Instagram login talab qilsa: `chrome`/`edge`/`firefox`. **Burner akkaunt** ishlating — asosiy akkauntingiz avtomatlashtirish sababli cheklanishi mumkin |

#### Bilim bazasi: mustaqil rejim vs Graphify bilan integratsiya

Bot ikki rejimda ishlashi mumkin, **hech qanday qo'shimcha sozlashsiz mustaqil
ishlaydi**:

- **Mustaqil rejim (standart, `GRAPHIFY_DIR` bo'sh bo'lganda).** Bot o'zining
  ichki ChromaDB bazasini yaratadi (`data/kb_db`). Hech narsa o'rnatish shart
  emas — klonlab, `.env`ni to'ldirib, darhol ishlatishingiz mumkin.
- **Graphify bilan integratsiya (ixtiyoriy).** Agar allaqachon
  [Graphify](https://github.com) (alohida, shaxsiy bilim bazasi vositasi)
  ishlatayotgan bo'lsangiz, `.env`da `GRAPHIFY_DIR` ni o'sha loyihaning
  papkasiga ko'rsating — bot saqlagan narsalar o'sha bazaga, boshqa
  loyihalaringiz bilan bir joyda tushadi.

`scripts/doctor.py` qaysi rejim faolligini va bazaning aniq joylashuvini
ko'rsatadi (pastga qarang).

### Ishga tushirish

```bash
.venv\Scripts\python -m src.bot
```

Yoki `run_bot.bat` ni ikki marta bosing (Windows).

Ishga tushirishdan oldin hamma narsa sozlanganini tekshirish uchun:

```bash
.venv\Scripts\python -m scripts.doctor
```

Bu ffmpeg, whisper (GPU/CPU), Ollama modellar, bilim bazasi backend'i, arxiv
papkasi va Telegram sozlamalarini tekshirib, aniq nima yetishmayotganini aytadi.

#### Kompyuter yoqilganda avtomatik ishga tushirish (Windows)

Har safar login qilganingizda bot avtomatik ishga tushishi va qulab tushsa
o'zi qayta ko'tarilishi uchun:

```powershell
$proj = (Get-Location).Path
$startup = [Environment]::GetFolderPath("Startup")
$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut((Join-Path $startup "InstagramGraphifyBot.lnk"))
$shortcut.TargetPath = "$proj\.venv\Scripts\pythonw.exe"
$shortcut.Arguments = "-m scripts.watchdog"
$shortcut.WorkingDirectory = $proj
$shortcut.WindowStyle = 7
$shortcut.Save()
```

Bu Windows'ning Startup papkasiga bir yorliq qo'yadi — u `scripts/watchdog.py`
(kichik "nazoratchi" skript)ni ishga tushiradi. Bu skript botni oynasiz ishga
tushiradi va u qulab tushsa (yoki har qanday sababdan chiqib ketsa) darhol
qayta ko'taradi, har bir qayta tushirishni `data/logs/watchdog.log`ga yozadi.
(Windows Task Scheduler — "rasmiy" yo'l, lekin ko'pincha korporativ domen
siyosati uni cheklab qo'yadi — Startup papkasi yorlig'i hech qanday maxsus
huquq talab qilmaydi va hamma joyda ishlaydi.)

Bot o'chiq turgan paytda yuborilgan xabarlar yo'qolmaydi: Telegram ularni
o'z serverida saqlab turadi, bot qayta ulanishi bilan ularni tartib bilan
qayta ishlaydi — qayta yuborish shart emas.

### Foydalanish (Telegram buyruqlari)

Botga `/start` yozib boshlang. Keyin:

| Nima yuborasiz | Natija |
|---|---|
| Instagram/YouTube linki | Navbatga qo'shiladi, tayyor bo'lgach xabar keladi |
| Video fayl | Xuddi shunday |
| Rasm(lar) (bitta yoki albom) + izoh | Xuddi shunday |
| Forward qilingan matn (link bilan) | Xuddi shunday |
| `/status` | Navbat holati va oxirgi ishlar |
| `/search <so'rov>` | Saqlangan bilimni qidiradi (masalan `/search prompt chaining`) |
| `/search all: <so'rov>` | Bilim bazasidagi **barcha** loyihalar bo'yicha qidiradi (Graphify rejimida foydali) |
| 🗑 tugmasi | Yozuvni **bazadan VA arxiv faylidan butunlay o'chiradi** — qaytarib bo'lmaydi |
| 🔄 tugmasi | Arxivdagi transkriptdan qayta tahlil qiladi (qayta yuklab olmaydi — tez) |

Bir vaqtda ko'p narsa tashlashingiz mumkin — bot ularni ketma-ket qayta ishlaydi
(GPU xotirasi cheklangani uchun parallel emas), har biri tayyor bo'lganda alohida
xabar keladi.

### Qanday ishlaydi (pipeline)

**Instagram/YouTube link yoki video fayl:**
```
yt-dlp yoki to'g'ridan fayl              (caption/tavsif ham olinadi)
  → ffmpeg → faster-whisper              (VAD filtri, avtomatik til aniqlash)
  → [transkript yupqa yoki yo'q bo'lsa] scene-change kadrlar → LLM vision (on-screen matn)
  → LLM → tasnif + xulosa + prompt/skill ajratish + qo'llash tavsiyasi
  → transkript/ajratilgan narsalar tasdiqdan o'tkaziladi (so'zma-so'z mosligi tekshiriladi)
  → [bazada o'xshash narsa bormi tekshiriladi]
  → data/archive/<kod>.md   ← haqiqat manbai
  → ChromaDB                ← qidiriladigan indeks
```

**Telegram post (rasm/video/matn, forward qilingan):**
```
Post
  → matndan havola(lar) ajratiladi (batafsil ma'lumot uchun saqlanadi)
  → rasm(lar) bo'lsa → LLM vision (on-screen matn)
  → xuddi shu tasnif/xulosa/ajratish/tekshirish jarayoni
  → xuddi shu MD arxiv + ChromaDB
```

Video ovozsiz bo'lsa (masalan qayta joylangan reel), transkripsiya bosqichi
o'tkazib yuboriladi va faqat kadrlar tahlil qilinadi — jarayon xato bermaydi.

### Rivojlantirish va sozlash

```bash
# Test to'plamida o'lchash (Telegram orqali bittalab sinashdan tezroq)
.venv\Scripts\python -m scripts.batch tests/fixtures --dry-run

# Faqat ajratish promptini sozlash (qayta yuklab olmasdan)
.venv\Scripts\python -m scripts.batch tests/fixtures --extract-only

# Arxivdan bazani noldan tiklash (embedding modelini almashtirgach kerak bo'ladi)
.venv\Scripts\python -m src.reindex --purge

# Unit testlar
.venv\Scripts\python -m pytest tests -q
```

`scripts/batch.py` alohida test navbati va bazadan foydalanadi — haqiqiy
Telegram navbatiga aralashmaydi.

### Xavfsizlik

- `.env` fayli **hech qachon commit qilinmaydi** (`.gitignore`da) — bot token va
  Telegram ID'ingiz shu yerda, maxfiy saqlanadi.
- `data/` papkasi ham commit qilinmaydi — real transkriptlar, chat ID'lar va
  navbat ma'lumotlari shu yerda.
- Reponi fork/klon qilsangiz, `.env.example`ni nusxalab, o'zingizning
  `BOT_TOKEN` va `ALLOWED_USER_IDS`ingizni kiriting.

### Cheklovlar

- Telegram Bot API fayl yuklashni 20MB bilan cheklaydi — kattaroq video bo'lsa,
  faylni emas, linkini tashlang.
- Instagram/YouTube login talab qilsa, `YTDLP_COOKIES_FROM_BROWSER` ni sozlang
  (burner akkaunt bilan).
- 8GB VRAM'li kartalarda whisper va LLM bir vaqtda to'liq sig'maydi — ketma-ket
  ishlaydi, whisper har ish oxirida xotirani bo'shatadi.
- Standart embedding modeli (`all-MiniLM-L6-v2`) faqat inglizchani yaxshi
  tushunadi — shuning uchun sarlavha/teglar inglizcha yoziladi (qidiruv
  langari o'shalar), tushuntirish matni esa o'zbekcha qoladi.

### Litsenziya

MIT — [LICENSE](LICENSE) faylga qarang.
