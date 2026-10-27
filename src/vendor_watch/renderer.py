"""Uzbek Telegram message renderer with strict <= 3900 char enforcement."""

import html

MAX_MESSAGE_CHARS = 3900


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
    """
    vendor = event.get("vendor", "")
    product = event.get("product", "")
    triage_type = event.get("triage_type", "")
    uz_title = enrichment.get("uz_title") or event.get("title", "")
    uz_summary = enrichment.get("uz_summary", "")
    uz_benefit = enrichment.get("uz_benefit", "")
    uz_how = enrichment.get("uz_how_to_use", "")
    overlap = enrichment.get("possible_overlap_with_custom_tool")

    parts = [
        f"🔔 <b>{html.escape(uz_title)}</b>",
        f"🏢 <i>{html.escape(vendor)} — {html.escape(product)}</i>" + (f" [{html.escape(triage_type)}]" if triage_type else ""),
        "",
        "📌 <b>Nima?</b>",
        html.escape(uz_summary),
        "",
        "🎯 <b>Sizga foydasi:</b>",
        html.escape(uz_benefit),
    ]

    if uz_how:
        parts.extend([
            "",
            "💻 <b>Qanday ishlatish:</b>",
            f"<code>{html.escape(uz_how)}</code>",
        ])

    # Warnings / Deprecations / Overlap
    warnings = []
    if triage_type == "deprecation":
        warnings.append("Ushbu xizmat/versiya yaqin orada to'xtatiladi (deprecation). Kodni yangilash tavsiya etiladi.")
    elif triage_type == "breaking":
        warnings.append("Orqaga moslik buzilgan (breaking changes). Mavjud integratsiyalarni qayta tekshiring.")
    elif triage_type == "security":
        warnings.append("Xavfsizlik zaifligi bartaraf etilgan. Zudlik bilan yangilash zarur.")

    if overlap and isinstance(overlap, dict):
        tool = overlap.get("tool_name", "")
        reason = overlap.get("reason", "")
        warnings.append(f"<b>{html.escape(tool)}</b> bilan ehtimoliy qisman o'xshashlik: {html.escape(reason)}")

    if warnings:
        parts.extend([
            "",
            "⚠️ <b>Ehtiyot bo'ling / Cheklov:</b>",
            "\n".join(f"• {w}" for w in warnings),
        ])

    # Sources & Links
    links = event.get("links") or ([event.get("link")] if event.get("link") else [])
    if links:
        parts.extend([
            "",
            "🔗 <b>Manbalar:</b>",
        ])
        for lnk in links:
            if lnk:
                parts.append(f"• <a href=\"{html.escape(lnk)}\">{html.escape(lnk)}</a>")

    full_text = "\n".join(parts)
    return truncate_lines(full_text, MAX_MESSAGE_CHARS)


def render_merged_high_priority(items: list[tuple[dict, dict]]) -> str:
    """Consolidate multiple high-priority updates when soft cap (5/day) is exceeded."""
    parts = [
        "🔔 <b>Vendor Updates — Yuqori Muhimlikdagi Yangilanishlar (Birlashtirilgan)</b>",
        "Bugungi muhim yangilanishlar hajmi ko'pligi sababli birlashtirildi.",
        "",
    ]
    for idx, (event, enrichment) in enumerate(items, 1):
        vendor = event.get("vendor", "")
        product = event.get("product", "")
        title = enrichment.get("uz_title") or event.get("title", "")
        summary = enrichment.get("uz_summary", "")
        benefit = enrichment.get("uz_benefit", "")
        link = event.get("link", "")

        parts.extend([
            f"<b>{idx}. {html.escape(vendor)} — {html.escape(product)}: {html.escape(title)}</b>",
            f"📌 <b>Nima:</b> {html.escape(summary)}",
            f"🎯 <b>Foydasi:</b> {html.escape(benefit)}",
            f"🔗 <b>Manba:</b> <a href=\"{html.escape(link)}\">{html.escape(link)}</a>" if link else "",
            "",
        ])

    return truncate_lines("\n".join(parts), MAX_MESSAGE_CHARS)


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
