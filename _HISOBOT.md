# Instagram Bot Konveyeri — Uchdan-Uchgacha Audit Hisoboti

- **Sana:** 2026-09-05
- **Audit obyekti:** `D:\claude projects\Instagram new Ideas` va `D:\claude projects\jamoa-sync.py`
- **Turi:** To'liq kod auditi (bo'shliqlar, yo'qotishlar, xatolarni yutish va chalg'ituvchi holatlar)

---

## 1. Asosiy Savollarga Javoblar

### 1. Qaysi bosqichda xato yuz bersa ish JIM tashlab yuboriladi?
- **Karusel postlarda video yuklash xatosi (`src/ingest.py:176-178`):** Agar postda ham rasmlar, ham video bo'lsa va rasmlar muvaffaqiyatli yuklansa, video yuklashdagi har qanday xatolik (`yt-dlp` istisnosi) butunlay yutiladi (`if not photo_paths: raise`). Foydalanuvchiga bu haqda xabar berilmaydi, transkripsiya qilinmaydi, faqat rasmlar tahlil qilinib "muvaffaqiyatli" saqlanadi.
- **Kadr tahlili (vision) bosqichidagi xatolar (`src/pipeline.py:326-328`, `337-339`, `405-407`):** Kadr ajratish (`extract_frames`) yoki LLM orqali o'qish (`read_frames`) yiqilsa (timeout bo'lmasa), xato faqat `log.warning` qilinadi va `vision_failed` bayrog'i bilan yutiladi. Natijada `onscreen` bo'sh qolib, matnli videolarning butun mag'zi boy beriladi.
- **Karusel rasmlarini yuklash (`src/ingest.py:160-161`):** Alohida rasmlarni yuklashda yuz bergan xatolar jimgina `except Exception: continue` bilan o'tkazib yuboriladi (hatto log ham yozilmaydi).
- **Kolleksiya metadatasini o'qish (`src/pipeline.py:475-490`):** `reprocess` paytida metadatalarni olish yiqilsa, `except Exception: pass` bilan yutiladi.

### 2. `usable=false` bo'lgan yozuv nima bo'ladi?
- **Arxivda qoladimi?** HA. `src/pipeline.py:176` da `archive.write(record)` orqali `.md` fayli yoziladi (frontmatter'da `usable: false`).
- **Graphify'ga tushadimi?** YO'Q. `src/pipeline.py:214-217` da `if record.get("usable"): ... else: record["kb_rows"] = 0` tekshiruvi orqali ChromaDB ga yozilmaydi.
- **Foydalanuvchi buni tushunarli biladimi?** Qisman. Telegramda `⚠️ shortcode — foydali mazmun topilmadi, bazaga saqlanmadi` degan bildirishnoma chiqadi (`src/worker.py:38-41`). Biroq:
  1) Agar bot arxivi `Promtlarim/instagram` ga yo'naltirilgan bo'lsa (`ARCHIVE_DIR`), `D:\claude projects\jamoa-sync.py:68-71` arxivdagi barcha `.md` fayllarni (shu jumladan `usable: false` bo'lganlarni ham) filtrsiz GitHub `promtlarim-jamoa` reposiga push qiladi, natijada jamoa reposi foydasiz yozuvlar bilan to'ladi.
  2) Agar foydalanuvchi xuddi shu havolani qayta yuborsa, `src/bot.py:76` "Bu allaqachon bazada" deb xabar beradi, holbuki u bilimlar bazasiga kiritilmagan.

### 3. Dublikat aniqlanganda oqim to'g'rimi?
- **Oqim noto'g'ri va ikki nusxa qoladi:**
  1) `src/pipeline.py:176` da `archive.write(record)` dublikat tekshiruvidan **OLDIN** chaqiriladi.
  2) 209–212 qatorlarda `record["flags"].append(f"possible_duplicate:{m['shortcode']}")` va `record["duplicate_of"] = m` qo'shiladi, ammo arxivga qayta yozilmaydi. Shuning uchun arxiv faylida (`.md`) dublikat bayrog'i aslo aks etmaydi.
  3) `src/pipeline.py:215` da yangi shortcode bilan `kb.upsert_record` bajariladi. Eski yozuv bazada qoladi, yangisi ham qo'shiladi. Natijada bazada ham, arxivda ham ikkita alohida nusxa saqlanib qoladi.

### 4. Bot ish o'rtasida qayta ishga tushsa nima yo'qoladi?
- **Qayta boshlanadigan bosqichlar:** `src/queue_db.py:198` da `running` holatidagi ishlar `queued` ga qaytariladi. Worker ishni olgach, `src/pipeline.py:421` hech qanday oraliq keshsiz jarayonni boshidan (download -> whisper -> vision -> extract -> kb) qayta boshlaydi.
- **Yarim qolgan va yo'qoladigan holatlar:**
  1) Agar ish 3-urinishda bo'lsa (`attempts >= 3`), `requeue_running` uni to'g'ridan-to'g'ri `failed` qilib qo'yadi. Foydalanuvchiga xato xabari yuborilmaydi (Telegramdagi xabar abadiy `⏳ Yuklanmoqda...` holatida qotadi).
  2) Forward qilingan fayl (`file`) yoki foto albom (`photo`) bo'lsa va oldingi urinishda `cleanup()` ishlab ulgurgan bo'lsa, manba fayl diskdan o'chirilgan bo'ladi. Qayta ishga tushganda `FileNotFoundError` bilan ish barbod bo'ladi.
  3) Agar bot `pipeline.process` yakunlanib, `queue_db.finish` chaqirilgunga qadar restart bo'lsa, Graphify va arxivga yozilgan natija ustiga qaytadan 15 daqiqalik og'ir model hisob-kitoblari yugurtiriladi.

### 5. `data/media/` va vaqtinchalik fayllar har holatda tozalanadimi?
- **YO'Q, diskda axlat qoladi:**
  1) `src/pipeline.py:361-419` (`_process_photo_or_text`) da `_process_video` da mavjud bo'lgan `try...except: cleanup(); raise` himoyasi yo'q. Agar foto tahlili paytida kadr o'qishda timeout xatosi yuz bersa (`queue_db.is_timeout_error` tufayli `raise` qilinganda), `cleanup()` chaqirilmaydi va `data/media/{sc}/photos` papkasi diskda qolib ketadi. (Eslatma: oddiy LLM xatoliklarida xato yutilib, `_finish` orqali `cleanup()` baribir ishlaydi; axlat faqat timeout xatoligida qoladi).
  2) `src/worker.py:189-222` da `keep_media=True` bilan saqlangan media fayllar faqat `if media_paths:` sharti bajarilsagina o'chiriladi. Agar ro'yxat bo'sh bo'lsa, `data/media/{sc}` papkasi umrbot diskda qoladi.
  3) Hozirgi `data/media/` papkasida 15 dan ortiq eski papkalar (masalan, `DbTFsUPjSz8`, `Dc12TWpM3yq`, `local-synthetic_slides`, `test1`, `test2` va o'nlab `tg-*`) to tozalanmasdan yotgani buning amaliy isbotidir.

### 6. `olchov.jsonl` ga yozuv HAR ishda tushadimi yoki faqat muvaffaqiyatlida?
- **Faqat `pipeline.process` ga kirgan ishlarda tushadi, ammo xatolar buzib yoziladi:**
  1) `src/pipeline.py:438-448` da xatolik yuz berganda `tracker.record_error()` va `tracker.write_to_file()` chaqiriladi.
  2) **Biroq jiddiy nuqson bor:** `pipeline.py:276` da `tracker.record_error("video_too_long")` chaqirilib, `VideoTooLongError` otiladi. Ammo 447-qatordagi `except Exception:` bloki yana parametrsiz `tracker.record_error()` ni chaqiradi. Natijada `pipeline.py:65` dagi fallback mantiq tufayli `self.xato = "yuklab_olish"` bo'lib qoladi. Video uzunligi tufayli yiqilgan barcha ishlar soxta ravishda "yuklab_olish" xatosi deb hisobotga kiradi.
  3) `src/pipeline.py:462` dagi `reprocess` funksiyasida `_OlchovTracker` umuman yo'q. Qayta ishlangan ishlarning sarflagan vaqti hisobga kirmaydi.
  4) `queue_db` ga tushmasdan oldin rad etilgan ishlar (katta fayl, domen xatosi) o'lchovga kirmaydi.

### 7. `jamoa-sync.py` 6 soatlik oralig'ida nima bo'ladi?
- **O'chirilgan fayllar:** Botda `🗑 O'chirish` bosilganda lokal arxivdan fayl darhol o'chadi (`src/bot.py:460`). Biroq `jamoa-sync.py` 6 soatlik intervalda ishlagani sababli, GitHub jamoa reposida bu fayl 6 soatgacha o'chmasdan turadi.
- **Yarim yozilgan (chala) fayllar xavfi:** `src/archive.py:92` faylni to'g'ridan-to'g'ri `p.write_text(...)` orqali yozadi (atomar `.tmp -> replace` emas). Agar aynan yozish paytida `jamoa-sync.py` ishga tushib qolsa (`jamoa-sync.py:88, 94`), chala yoki bo'sh fayl GitHub repoga `git commit` va `git push` qilib yuboriladi.

### 8. `src/kb.py` `_retry`: `max_reopen = 1` yetarlimi?
- **YETARLI EMAS:**
  1) `max_reopen = 1` qilib belgilangan (`src/kb.py:117`).
  2) Agar kolleksiya eskirgan bo'lsa (`_is_stale_error`), `_reopen()` chaqiriladi (134-qator). Ammo bu chaqiriq oldidan yoki keyin **hech qanday kutish vaqti (`time.sleep`) yo'q**.
  3) Tashqi jarayon (Graphify MCP yoki reindex) bazaga yozayotgan paytda millisekundlar ichida qayta urinish yana eskirgan UUID ga to'qnash keladi va 131-qatorda `reopen_attempts > 1` bo'lib darhol `raise` qilinadi.
  4) SQLite uchun `max_locked = 5` va kutish bor, lekin ChromaDB uchun kutishsiz 1 ta urinish tashqi faol indekslash paytida GPU da bajarilgan 15 daqiqalik ishni to'liq barbod qiladi.

---

## 2. Bo'shliqlar Ro'yxati (Og'irlik darajasi bo'yicha)

### [YUQORI] 1. Arxivsiz yiqilgan ish qayta yuborilganda bloklanishi (`INSERT OR IGNORE`)
- **Fayl va qator:** `src/queue_db.py:67-73` va `src/bot.py:224-226`, `328-330`, `381-383`, `438-440`
- **Koddan iqtibos:**
  ```python
  # src/queue_db.py:67-73
  cur = con.execute(
      "INSERT OR IGNORE INTO jobs "
      "(id, shortcode, source_type, source, chat_id, message_id, created_at, updated_at, author_id, author_name) "
      "VALUES (?,?,?,?,?,?,?,?,?,?)",
      (job_id, shortcode, source_type, source, chat_id, message_id, now, now, str(author_id or ""), str(author_name or "")),
  )
  return cur.rowcount > 0
  ```
  ```python
  # src/bot.py:224-226
  added = queue_db.enqueue(f"{prefix}:{shortcode}", shortcode, "url", url,
                           message.chat.id, status.message_id,
                           author_id=author_id, author_name=author_name)
  if not added:
      await _deliver_existing(status, shortcode)
  ```
- **Oqibati:** Agar video birinchi urinishda yuklab olish (download) bosqichida tarmoq yoki boshqa sabab bilan `failed` bo'lsa (arxiv `.md` fayli hali yaratilmagan paytda), foydalanuvchi uni qayta yuborganida `INSERT OR IGNORE` tufayli yangi ish qo'shilmaydi (`added=False`). Bot `_deliver_existing` ni chaqiradi va "allaqachon bazada (arxiv fayli topilmadi)" deb xabar beradi, 🔄 tugmasi ham berilmaydi. Natijada arxivsiz yiqilgan ishni Telegramdan qayta yuborib bo'lmaydi va ish botib qoladi. (Eslatma: agar arxiv fayli yaratilib bo'lingandan keyingi bosqichlarda yiqilsa, 🔄 Qayta ishlash tugmasi orqali `reprocess` ni ishga tushirish imkoni saqlanadi).

---

### [YUQORI] 2. Karusel postlarda video yuklash xatosi jimgina yutilishi
- **Fayl va qator:** `src/ingest.py:176-178`
- **Koddan iqtibos:**
  ```python
  # src/ingest.py:176-178
          except Exception as e:
              if not photo_paths:
                  raise DownloadError(str(e)) from e
  ```
- **Oqibati:** Agar Instagram postida ham video, ham rasmlar bo'lsa va rasmlar yuklab olingach video yuklashda xatolik chiqsa (`yt_dlp` yiqilsa), `if not photo_paths` sharti tufayli xatolik to'liq yutiladi. Foydalanuvchiga ogohlantirish berilmaydi, video tahlil qilinmaydi, faqat rasm saqlanadi. Ma'lumot yo'qoladi.

---

### [YUQORI] 3. Dublikat bayrog'i arxivga yozilmasligi va bazada ikki nusxa qolishi
- **Fayl va qator:** `src/pipeline.py:176`, `src/pipeline.py:209-215`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:176
      md_path = archive.write(record)
      record["md_path"] = str(md_path)
  ```
  ```python
  # src/pipeline.py:209-215
                  if matches and matches[0]["similarity"] >= config.DUPLICATE_SIMILARITY_THRESHOLD:
                      m = matches[0]
                      record["flags"].append(f"possible_duplicate:{m['shortcode']}")
                      record["duplicate_of"] = m

              if record.get("usable"):
                  record["kb_rows"] = kb.upsert_record(record, author_id=author_id, author_name=author_name)
  ```
- **Oqibati:** `archive.write` dublikat aniqlanishidan oldin (176-qatorda) bajariladi va keyin qayta chaqirilmaydi. Asosiy haqiqat manbai bo'lgan `.md` arxiv faylida dublikat haqida hech narsa saqlanmaydi. Shu bilan birga, 215-qatorda yangi yozuv baribir ChromaDB ga yoziladi va bazada ikki nusxa dublikat paydo bo'ladi.

---

### [YUQORI] 4. Bot qayta ishga tushganda ishlarning jimgina to'xtab qolishi
- **Fayl va qator:** `src/queue_db.py:198-200`, `src/bot.py:750-752`
- **Koddan iqtibos:**
  ```python
  # src/queue_db.py:198-200
              "  status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'queued' END, "
              "  stage  = CASE WHEN attempts >= ? THEN stage ELSE 'queued' END, "
              "  updated_at = ? "
              "WHERE status='running'",
              (MAX_ATTEMPTS, MAX_ATTEMPTS, time.time()),
  ```
- **Oqibati:** Agar ish 3-urinishda ishlayotganda bot restart bo'lsa, ish jimgina `failed` holatiga o'tadi. `worker.py` xato yetkazish blokidan o'tmagani uchun foydalanuvchiga Telegramda hech qanday xabar bormaydi (navbat xabari abadiy qotib qoladi). Agar fayl yoki foto bo'lsa, manba fayllar oldingi urinishda o'chib ketgan bo'lsa, qayta urinish darhol xato bilan tugaydi.

---

### [YUQORI] 5. Arxiv faylini noatomar yozish va Git sinxronlash to'qnashuvi
- **Fayl va qator:** `src/archive.py:92`, `D:\claude projects\jamoa-sync.py:88, 94`
- **Koddan iqtibos:**
  ```python
  # src/archive.py:91-93
      p = path_for(record["shortcode"])
      p.write_text(render(record), encoding="utf-8")
      return p
  ```
  ```python
  # D:\claude projects\jamoa-sync.py:90-94
              if not filecmp.cmp(src_path, dst_path, shallow=False):
                  modified.append(dst_path)
                  if not dry_run:
                      dst_path.parent.mkdir(parents=True, exist_ok=True)
                      shutil.copy2(src_path, dst_path)
  ```
- **Oqibati:** `p.write_text` faylni vaqtinchalik faylsiz to'g'ridan-to'g'ri yozadi. Ayni shu paytda `jamoa-sync.py` ishlab qolsa, chala yozilgan yoki 0 baytli faylni nusxalab, GitHub repoga commit va push qilib yuboradi. Natijada umumiy repoda buzuq ma'lumot saqlanadi.

---

### [O'RTA] 6. `kb.py` `_retry` da `max_reopen = 1` va kutish yo'qligi
- **Fayl va qator:** `src/kb.py:117`, `130-136`
- **Koddan iqtibos:**
  ```python
  # src/kb.py:117, 130-136
      max_reopen = 1
  ...
              elif _is_stale_error(e):
                  reopen_attempts += 1
                  if reopen_attempts > max_reopen:
                      raise
                  log.warning("ChromaDB kolleksiyasi eskirgan (%s), qayta ochilib qayta urinilmoqda...", e)
                  new_col = _reopen()
                  if hasattr(fn, "__self__") and hasattr(fn, "__name__"):
                      fn = getattr(new_col, fn.__name__)
  ```
- **Oqibati:** Kolleksiya eskirganida `_reopen()` chaqiriladi, ammo hech qanday pauzasiz darhol qayta urinadi. Tashqi Graphify MCP faol yozayotgan bo'lsa, ikkinchi urinish ham xato berib, pipeline to'liq yiqiladi va barcha oldingi bosqichlar (whisper/vision/extract) mehnati zoye ketadi.

---

### [O'RTA] 7. `_process_photo_or_text` timeout bilan yiqilganda tozalash yo'qligi (disk axlati)
- **Fayl va qator:** `src/pipeline.py:361-419`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:412-414
      def cleanup(_preserve_media: bool = False):
          if job["source_type"] == "photo":
              shutil.rmtree(job["source"], ignore_errors=True)
  ```
- **Oqibati:** `_process_video` da bo'lgan `try ... except: cleanup(); raise` o'rami bu yerda mavjud emas. Agar foto albomni tahlil qilishda kadr o'qish jarayoni timeout bilan yiqilsa (`queue_db.is_timeout_error` tufayli `raise` bo'lganda), `cleanup()` chaqirilmaydi va `data/media/{sc}/photos` papkasi diskda qolib ketadi. Biroq oddiy LLM xatoliklarida xato `flags` ga yozilib yutiladi va `_finish` orqali `cleanup()` baribir ishlaydi, ya'ni disk axlati faqat timeout holatida to'planadi.

---

### [O'RTA] 8. Kadr tahlili (vision) xatoliklarining yutilishi
- **Fayl va qator:** `src/pipeline.py:326-328`, `337-339`, `405-407`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:334-339
                  try:
                      record["onscreen"] = extract.read_frames(vision_paths)
                  except Exception as e:
                      if queue_db.is_timeout_error(e):
                          raise
                      log.warning("vision stage failed for %s: %s", sc, e)
                      record["flags"].append("vision_failed")
  ```
- **Oqibati:** Agar videodagi kadrlar tahlili yiqilsa, xato yutiladi va `onscreen` bo'sh qoladi. Nutqsiz, faqat matnli videolarda LLM ga hech qanday matn bormaydi, oqibatda `usable=false` bo'lib video tashlab yuboriladi.

---

### [O'RTA] 9. `usable=false` yozuvlarning Git repoga asossiz chiqib ketishi
- **Fayl va qator:** `src/pipeline.py:176`, `src/pipeline.py:214-217`, `D:\claude projects\jamoa-sync.py:68-71`
- **Koddan iqtibos:**
  ```python
  # D:\claude projects\jamoa-sync.py:68-71
      for f in src_dir.rglob("*"):
          if f.is_file() and f.suffix.lower() in ALLOWED_EXTS:
              rel = f.relative_to(src_dir)
              src_map[rel] = f
  ```
- **Oqibati:** Foydasiz deb topilgan videolar (`usable: false`) Graphify'ga kirmaydi, ammo `.md` fayli arxivda saqlanadi. Agar bot arxivi `Promtlarim/instagram` ga sozlangan bo'lsa (`config.ARCHIVE_DIR`), `jamoa-sync.py` bu fayllarni filtrsiz GitHub repoga nusxalab push qiladi. (Eslatma: Agar arxiv standart `data/archive` da bo'lsa, `jamoa-sync.py` ning `DEFAULT_SOURCE` papkasi bilan mos kelmaydi va Git'ga bevosita chiqmaydi — bu konfiguratsiyaga bog'liq).

---

### [O'RTA] 10. `_OlchovTracker` xato nomini soxtalashtirishi
- **Fayl va qator:** `src/pipeline.py:276`, `src/pipeline.py:447`, `src/pipeline.py:65`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:275-277
          if tracker:
              tracker.record_error("video_too_long")
          raise VideoTooLongError(...)
  ...
  # src/pipeline.py:446-448
      except Exception as e:
          if tracker:
              tracker.record_error()
              tracker.write_to_file()
  ```
  ```python
  # src/pipeline.py:65-67
          st = stage or self.current_stage or "yuklab_olish"
          self.end_stage(st)
          self.xato = st
  ```
- **Oqibati:** 276-qatorda `video_too_long` yozilgani bilan, 447-qatorda parametrsiz chaqiruv uni `yuklab_olish` ga almashtirib yuboradi (`current_stage` bo'sh bo'lgani sababli). Natijada `olchov.jsonl` dagi statistika yolg'on bo'ladi.

---

### [PAST] 11. Karusel postlardagi ayrim rasmlar tushib qolishi
- **Fayl va qator:** `src/ingest.py:160-161`
- **Koddan iqtibos:**
  ```python
  # src/ingest.py:152-161
          try:
              photo_url = entry["thumbnails"][-1]["url"]
              ...
              photo_paths.append(path)
          except Exception:
              continue
  ```
- **Oqibati:** Alohida rasmlar yuklanmay qolsa, xato jimgina yutiladi, hech qanday log yo'q. Qaysi slaydlar tushib qolgani noma'lum bo'ladi.

---

### [PAST] 12. `jamoa-sync.py` 6 soatlik oraliqdagi o'chirish kechikishi
- **Fayl va qator:** `src/bot.py:460`, `D:\claude projects\jamoa-sync.py:289-298`
- **Koddan iqtibos:**
  ```python
  # D:\claude projects\jamoa-sync.py:289
      interval_sec = max(60.0, args.interval * 3600.0)
  ```
- **Oqibati:** Botdan o'chirilgan fayl GitHub jamoa reposidan faqat 6 soatdan keyin o'chiriladi.

---

### [PAST] 13. `reprocess` amali `olchov.jsonl` ga kirmasligi
- **Fayl va qator:** `src/pipeline.py:462-498`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:462
  def reprocess(shortcode: str, author_id: str = "", author_name: str = "") -> dict:
  ```
- **Oqibati:** Qayta ishlash amallarida tracker ishlatilmagani uchun resurs sarfi vaqt statistikasi umumiy o'lchovga qo'shilmaydi.

---

### [PAST] 14. `reprocess` paytida ChromaDB metadatasini o'qish yiqilsa mualliflar yo'qolishi
- **Fayl va qator:** `src/pipeline.py:475-490`
- **Koddan iqtibos:**
  ```python
  # src/pipeline.py:489-490
      except Exception:
          pass
  ```
- **Oqibati:** Reprocess paytida ChromaDB o'qishda xato yuz bersa, oldingi muallif va hissa qo'shuvchilar tarixi (`contributors`) yo'qoladi.


