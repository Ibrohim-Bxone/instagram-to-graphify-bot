"""Uzbek Telegram message renderer with strict <= 3900 char enforcement."""

import html

MAX_MESSAGE_CHARS = 3900


def truncate_field(text: str, max_chars: int, suffix: str = "...") -> str:
    """Safely truncate plain text before escaping/wrapping into HTML tags."""
    if not text or len(text) <= max_chars:
        return text
    budget = max_chars - len(suffix)
    if budget <= 0:
        return text[:max_chars]
    return text[:budget].rstrip() + suffix


def truncate_lines(text: str, limit: int = MAX_MESSAGE_CHARS, hint: str = "...\n(qisqartirildi)") -> str:
    """Trim text on line boundary to fit strictly within limit."""
    if len(text) <= limit:
        return text

    budget = limit - len(hint) - 2
    if budget <= 0:
        return text[:limit]

    lines = text.splitlines(keepends=True)
    accum: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) > budget:
            remaining = budget - total
            if remaining > 0:
                accum.append(line[:remaining])
            break
        accum.append(line)
        total += len(line)

    res = "".join(accum).rstrip()
    if not res:
        res = text[:budget]
    return f"{res}\n\n{hint}"


def render_high_priority_message(event: dict, enrichment: dict) -> str:
    """Render a single high-priority update in the required Uzbek layout.

    Structure:
    Nima? → Sizga foydasi → Qanday ishlatish → Ehtiyot bo'ling/cheklov → Manbalar.
    Truncates field texts before escaping/wrapping into HTML, never the rendered HTML.
    """
    vendor = event.get("vendor", "")
    product = event.get("product", "")
    triage_type = event.get("triage_type", "")
    if triage_type == "account_change":
        uz_title = event.get("title") or enrichment.get("uz_title", "")
        uz_summary = enrichment.get("uz_summary") or event.get("raw_content", "")
        uz_benefit = enrichment.get("uz_benefit") or event.get("profile_impact", "")
        uz_how = enrichment.get("uz_how_to_use") or event.get("command", "")
    else:
        uz_title = enrichment.get("uz_title") or event.get("title", "")
        uz_summary = enrichment.get("uz_summary", "")
        uz_benefit = enrichment.get("uz_benefit", "")
        uz_how = enrichment.get("uz_how_to_use", "")
    overlap = enrichment.get("possible_overlap_with_custom_tool")

    clean_title = truncate_field(uz_title, 200)
    clean_vendor = truncate_field(vendor, 80)
    clean_product = truncate_field(product, 80)

    # Warnings / Deprecations / Overlap
    warnings = []
    if triage_type == "deprecation":
        warnings.append("Ushbu xizmat/versiya yaqin orada to'xtatiladi (deprecation). Kodni yangilash tavsiya etiladi.")
    elif triage_type == "breaking":
        warnings.append("Orqaga moslik buzilgan (breaking changes). Mavjud integratsiyalarni qayta tekshiring.")
    elif triage_type == "security":
        warnings.append("Xavfsizlik zaifligi bartaraf etilgan. Zudlik bilan yangilash zarur.")

    if overlap and isinstance(overlap, dict):
        tool = truncate_field(overlap.get("tool_name", ""), 80)
        reason = truncate_field(overlap.get("reason", ""), 250)
        warnings.append(f"<b>{html.escape(tool)}</b> bilan ehtimoliy qisman o'xshashlik: {html.escape(reason)}")

    # Sources & Links
    links = event.get("links") or ([event.get("link")] if event.get("link") else [])
    link_lines = []
    for lnk in links[:5]:
        if lnk:
            cl = lnk.strip()
            esc_cl = html.escape(cl)
            if len(esc_cl) <= 250 and len(cl) <= 200:
                short_lbl = truncate_field(cl, 80)
                link_lines.append(f'• <a href="{esc_cl}">{html.escape(short_lbl)}</a>')
            else:
                short_lbl = truncate_field(cl, 80)
                link_lines.append(f'• {html.escape(short_lbl)}')

    # Calculate budget for variable content (summary, benefit, how_to)
    static_len = len(clean_title) + len(clean_vendor) + len(clean_product) + 200
    for w in warnings:
        static_len += len(w) + 10
    for l in link_lines:
        static_len += len(l) + 10

    available_budget = max(300, MAX_MESSAGE_CHARS - static_len)

    how_budget = min(300, len(uz_how)) if uz_how else 0
    remaining = available_budget - how_budget
    benefit_budget = min(500, max(120, remaining // 3))
    summary_budget = max(180, remaining - benefit_budget)

    clean_summary = truncate_field(uz_summary, summary_budget)
    clean_benefit = truncate_field(uz_benefit, benefit_budget)
    clean_how = truncate_field(uz_how, how_budget) if uz_how else ""

    def _build_message(title_txt: str, summary_txt: str, benefit_txt: str, how_txt: str) -> str:
        p = [
            f"🔔 <b>{html.escape(title_txt)}</b>",
            f"🏢 <i>{html.escape(clean_vendor)} — {html.escape(clean_product)}</i>" + (f" [{html.escape(triage_type)}]" if triage_type else ""),
            "",
            "📌 <b>Nima?</b>",
            html.escape(summary_txt),
            "",
            "🎯 <b>Sizga foydasi:</b>",
            html.escape(benefit_txt),
        ]
        if how_txt:
            p.extend([
                "",
                "💻 <b>Qanday ishlatish:</b>",
                f"<code>{html.escape(how_txt)}</code>",
            ])
        if warnings:
            p.extend([
                "",
                "⚠️ <b>Ehtiyot bo'ling / Cheklov:</b>",
                "\n".join(f"• {w}" if not w.startswith("<b>") else w for w in warnings),
            ])
        if link_lines:
            p.extend([
                "",
                "🔗 <b>Manbalar:</b>",
                *link_lines,
            ])
        return "\n".join(p)

    full_text = _build_message(clean_title, clean_summary, clean_benefit, clean_how)

    # If escaped tags / entities push length over limit, iteratively truncate field text BEFORE escaping
    while len(full_text) > MAX_MESSAGE_CHARS:
        excess = len(full_text) - MAX_MESSAGE_CHARS
        if len(clean_summary) > 100:
            reduction = min(len(clean_summary) - 80, excess + 20)
            clean_summary = truncate_field(clean_summary, len(clean_summary) - reduction)
        elif len(clean_benefit) > 60:
            reduction = min(len(clean_benefit) - 50, excess + 20)
            clean_benefit = truncate_field(clean_benefit, len(clean_benefit) - reduction)
        elif clean_how and len(clean_how) > 40:
            reduction = min(len(clean_how) - 30, excess + 20)
            clean_how = truncate_field(clean_how, len(clean_how) - reduction)
        elif len(clean_title) > 50:
            reduction = min(len(clean_title) - 40, excess + 20)
            clean_title = truncate_field(clean_title, len(clean_title) - reduction)
        elif len(link_lines) > 1:
            link_lines.pop()
        elif warnings:
            warnings.pop()
        else:
            break
        full_text = _build_message(clean_title, clean_summary, clean_benefit, clean_how)

    return full_text


def render_account_change_message(event: dict, enrichment: dict | None = None) -> str:
    """Render a tier-3 account probe change in the required Uzbek layout.

    Layout:
    🔔 <b>Sizning akkauntingizda: <model> paydo bo'ldi / yo'qoldi</b>
    🏢 <i>Vendor — Product</i> [account_change]
    📌 <b>Nima?</b> ...
    🎯 <b>Sizga foydasi:</b> (profile.md stack impact)
    💻 <b>Qanday ishlatish:</b> exact command
    """
    enr = enrichment or {
        "uz_title": event.get("title", ""),
        "uz_summary": event.get("raw_content") or event.get("summary", ""),
        "uz_benefit": event.get("profile_impact", ""),
        "uz_how_to_use": event.get("command", ""),
        "possible_overlap_with_custom_tool": None,
    }
    return render_high_priority_message(event, enr)



def render_merged_pages(items: list[tuple[dict, dict]]) -> list[tuple[str, list[tuple[dict, dict]]]]:
    """Consolidate multiple high-priority updates when soft cap (5/day) is exceeded.

    Splits into several bounded messages (<= 3900 chars) ensuring every event is contained.
    Returns a list of tuples: (rendered_message, list_of_items_in_this_page).
    """
    if not items:
        return []

    header = "🔔 <b>Vendor Updates — Yuqori Muhimlikdagi Yangilanishlar (Birlashtirilgan)</b>\n" \
             "Bugungi muhim yangilanishlar hajmi ko'pligi sababli birlashtirildi.\n\n"

    pages: list[tuple[str, list[tuple[dict, dict]]]] = []
    current_parts = [header]
    current_items: list[tuple[dict, dict]] = []
    current_len = len(header)

    for idx, (event, enrichment) in enumerate(items, 1):
        vendor = truncate_field(event.get("vendor", ""), 50)
        product = truncate_field(event.get("product", ""), 50)
        raw_title = enrichment.get("uz_title") or event.get("title", "")
        title = truncate_field(raw_title, 120)
        summary = truncate_field(enrichment.get("uz_summary", ""), 250)
        benefit = truncate_field(enrichment.get("uz_benefit", ""), 150)
        link = event.get("link", "")

        def _build_block(t_txt: str, s_txt: str, b_txt: str) -> str:
            parts = [
                f"<b>{idx}. {html.escape(vendor)} — {html.escape(product)}: {html.escape(t_txt)}</b>",
                f"📌 <b>Nima:</b> {html.escape(s_txt)}",
                f"🎯 <b>Foydasi:</b> {html.escape(b_txt)}",
            ]
            if link:
                cl = link.strip()
                esc_cl = html.escape(cl)
                if len(esc_cl) <= 250 and len(cl) <= 200:
                    short_lbl = truncate_field(cl, 80)
                    parts.append(f'🔗 <b>Manba:</b> <a href="{esc_cl}">{html.escape(short_lbl)}</a>')
                else:
                    short_lbl = truncate_field(cl, 80)
                    parts.append(f'🔗 <b>Manba:</b> {html.escape(short_lbl)}')
            parts.append("")
            return "\n".join(parts) + "\n"

        block = _build_block(title, summary, benefit)
        while len(block) > 900:
            if len(summary) > 80:
                summary = truncate_field(summary, len(summary) - 40)
            elif len(benefit) > 60:
                benefit = truncate_field(benefit, len(benefit) - 30)
            elif len(title) > 50:
                title = truncate_field(title, len(title) - 20)
            else:
                break
            block = _build_block(title, summary, benefit)

        cont_header = "🔔 <b>Vendor Updates — Birlashtirilgan yangilanishlar (davomi)</b>\n\n"
        if current_len + len(block) > (MAX_MESSAGE_CHARS - 50):
            if current_items:
                pages.append(("\n".join(current_parts).strip(), current_items))
            current_parts = [cont_header, block]
            current_items = [(event, enrichment)]
            current_len = len(cont_header) + len(block)
        else:
            current_parts.append(block)
            current_items.append((event, enrichment))
            current_len += len(block)

    if current_items:
        pages.append(("\n".join(current_parts).strip(), current_items))

    return [(m, itms) for m, itms in pages if m.strip()]


def render_merged_high_priority(items: list[tuple[dict, dict]]) -> list[str]:
    return [page[0] for page in render_merged_pages(items)]


def render_digest_pages(
    items: list[tuple[dict, dict]],
    ambiguous_count: int = 0,
) -> list[tuple[str, list[tuple[dict, dict]]]]:
    """Render regular updates into one or more digest messages <= 3900 chars.

    Returns a list of tuples: (rendered_message, list_of_items_in_this_page).
    """
    if not items:
        if ambiguous_count > 0:
            msg = "📋 <b>Vendor Updates — Kunlik Dayjest</b>\n" \
                  f"⚠️ Eslatma: {ambiguous_count} ta yetkazib berish holati noaniq (ambiguous).\n" \
                  "Bugungi yangi relizlar mavjud emas.\n"
            return [(msg, [])]
        return []

    ambiguous_line = (
        f"⚠️ Eslatma: {ambiguous_count} ta yetkazib berish holati noaniq (ambiguous).\n"
        if ambiguous_count > 0
        else ""
    )
    header = "📋 <b>Vendor Updates — Kunlik Dayjest</b>\n" \
             f"{ambiguous_line}" \
             "So'nggi tizimli yangilanishlar va kutubxona relizlari:\n\n"

    pages: list[tuple[str, list[tuple[dict, dict]]]] = []
    current_parts = [header]
    current_items: list[tuple[dict, dict]] = []
    current_len = len(header)

    for idx, (event, enrichment) in enumerate(items, 1):
        vendor = truncate_field(event.get("vendor", ""), 50)
        product = truncate_field(event.get("product", ""), 50)
        raw_title = enrichment.get("uz_title") or event.get("title", "")
        title = truncate_field(raw_title, 120)
        raw_summary = enrichment.get("uz_summary", "")
        summary = truncate_field(raw_summary, 250)
        links = event.get("links") or ([event.get("link")] if event.get("link") else [])
        primary_link = links[0].strip() if links and links[0] else ""

        def _build_block(t_txt: str, s_txt: str) -> str:
            b = f"🔹 <b>{idx}. {html.escape(vendor)} ({html.escape(product)})</b>: {html.escape(t_txt)}\n" \
                f"   • {html.escape(s_txt)}\n"
            if primary_link:
                esc_link = html.escape(primary_link)
                if len(esc_link) <= 250 and len(primary_link) <= 200:
                    b += f'   • <a href="{esc_link}">Havola</a>\n'
                else:
                    short_lbl = truncate_field(primary_link, 80)
                    b += f'   • Havola: {html.escape(short_lbl)}\n'
            b += "\n"
            return b

        block = _build_block(title, summary)
        while len(block) > 800:
            if len(summary) > 80:
                summary = truncate_field(summary, len(summary) - 40)
            elif len(title) > 50:
                title = truncate_field(title, len(title) - 20)
            else:
                break
            block = _build_block(title, summary)

        cont_header = "📋 <b>Vendor Updates — Dayjest (davomi)</b>\n\n"
        if current_len + len(block) > (MAX_MESSAGE_CHARS - 50):
            if current_items:
                pages.append(("".join(current_parts).strip(), current_items))
            current_parts = [cont_header, block]
            current_items = [(event, enrichment)]
            current_len = len(cont_header) + len(block)
        else:
            current_parts.append(block)
            current_items.append((event, enrichment))
            current_len += len(block)

    if current_items:
        pages.append(("".join(current_parts).strip(), current_items))

    return [(m, itms) for m, itms in pages if m.strip()]


def render_digest_messages(items: list[tuple[dict, dict]], ambiguous_count: int = 0) -> list[str]:
    return [page[0] for page in render_digest_pages(items, ambiguous_count=ambiguous_count)]
