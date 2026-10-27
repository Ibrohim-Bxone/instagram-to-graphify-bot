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
            link_lines.append(f"• <a href=\"{html.escape(cl)}\">{html.escape(cl)}</a>")

    # Calculate budget for variable content (summary, benefit, how_to)
    static_len = len(clean_title) + len(clean_vendor) + len(clean_product) + 200
    for w in warnings:
        static_len += len(w) + 10
    for l in link_lines:
        static_len += len(l) + 10

    available_budget = max(400, MAX_MESSAGE_CHARS - static_len)

    how_budget = min(400, len(uz_how)) if uz_how else 0
    remaining = available_budget - how_budget
    benefit_budget = min(600, max(150, remaining // 3))
    summary_budget = max(200, remaining - benefit_budget)

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
        if len(clean_summary) > 200:
            reduction = min(len(clean_summary) - 150, excess + 20)
            clean_summary = truncate_field(clean_summary, len(clean_summary) - reduction)
        elif len(clean_benefit) > 100:
            reduction = min(len(clean_benefit) - 80, excess + 20)
            clean_benefit = truncate_field(clean_benefit, len(clean_benefit) - reduction)
        elif clean_how and len(clean_how) > 50:
            reduction = min(len(clean_how) - 40, excess + 20)
            clean_how = truncate_field(clean_how, len(clean_how) - reduction)
        else:
            break
        full_text = _build_message(clean_title, clean_summary, clean_benefit, clean_how)

    return full_text


def render_merged_high_priority(items: list[tuple[dict, dict]]) -> list[str]:
    """Consolidate multiple high-priority updates when soft cap (5/day) is exceeded.

    Splits into several bounded messages (<= 3900 chars) ensuring every event is contained.
    """
    if not items:
        return []

    header = "🔔 <b>Vendor Updates — Yuqori Muhimlikdagi Yangilanishlar (Birlashtirilgan)</b>\n" \
             "Bugungi muhim yangilanishlar hajmi ko'pligi sababli birlashtirildi.\n\n"

    messages: list[str] = []
    current_parts = [header]
    current_len = len(header)

    for idx, (event, enrichment) in enumerate(items, 1):
        vendor = truncate_field(event.get("vendor", ""), 60)
        product = truncate_field(event.get("product", ""), 60)
        raw_title = enrichment.get("uz_title") or event.get("title", "")
        title = truncate_field(raw_title, 150)
        summary = truncate_field(enrichment.get("uz_summary", ""), 300)
        benefit = truncate_field(enrichment.get("uz_benefit", ""), 200)
        link = event.get("link", "")

        block_parts = [
            f"<b>{idx}. {html.escape(vendor)} — {html.escape(product)}: {html.escape(title)}</b>",
            f"📌 <b>Nima:</b> {html.escape(summary)}",
            f"🎯 <b>Foydasi:</b> {html.escape(benefit)}",
        ]
        if link:
            block_parts.append(f"🔗 <b>Manba:</b> <a href=\"{html.escape(link)}\">{html.escape(link)}</a>")
        block_parts.append("")

        block = "\n".join(block_parts) + "\n"

        if current_len + len(block) > (MAX_MESSAGE_CHARS - 50):
            messages.append("\n".join(current_parts).strip())
            cont_header = "🔔 <b>Vendor Updates — Birlashtirilgan yangilanishlar (davomi)</b>\n\n"
            current_parts = [cont_header, block]
            current_len = len(cont_header) + len(block)
        else:
            current_parts.append(block)
            current_len += len(block)

    if current_parts:
        messages.append("\n".join(current_parts).strip())

    return messages


def render_digest_messages(items: list[tuple[dict, dict]]) -> list[str]:
    """Render regular updates (SDK patches, minor features) into one or more digest messages <= 3900 chars."""
    if not items:
        return []

    header = "📋 <b>Vendor Updates — Kunlik Dayjest</b>\n" \
             "So'nggi tizimli yangilanishlar va kutubxona relizlari:\n\n"

    messages: list[str] = []
    current_parts = [header]
    current_len = len(header)

    for idx, (event, enrichment) in enumerate(items, 1):
        vendor = event.get("vendor", "")
        product = event.get("product", "")
        title = enrichment.get("uz_title") or event.get("title", "")
        summary = enrichment.get("uz_summary", "")
        if len(summary) > 300:
            summary = summary[:297] + "..."
        links = event.get("links") or ([event.get("link")] if event.get("link") else [])
        primary_link = links[0] if links else ""

        block = f"🔹 <b>{idx}. {html.escape(vendor)} ({html.escape(product)})</b>: {html.escape(title)}\n" \
                f"   • {html.escape(summary)}\n"
        if primary_link:
            block += f"   • <a href=\"{html.escape(primary_link)}\">Havola</a>\n"
        block += "\n"

        if current_len + len(block) > (MAX_MESSAGE_CHARS - 50):
            messages.append(truncate_lines("".join(current_parts).strip(), MAX_MESSAGE_CHARS))
            current_parts = [f"📋 <b>Vendor Updates — Dayjest (davomi)</b>\n\n", block]
            current_len = len(current_parts[0]) + len(block)
        else:
            current_parts.append(block)
            current_len += len(block)

    if current_parts:
        messages.append(truncate_lines("".join(current_parts).strip(), MAX_MESSAGE_CHARS))

    return messages
