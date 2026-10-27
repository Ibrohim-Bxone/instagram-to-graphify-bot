"""Stdlib XML parser for Atom and RSS feeds."""

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import re
import xml.etree.ElementTree as ET


class FeedParseError(Exception):
    """Raised when an Atom or RSS feed cannot be parsed."""
    pass


@dataclass
class FeedEntry:
    entry_id: str
    title: str
    link: str
    published_at: str
    summary: str
    raw_content: str


def strip_html(text: str) -> str:
    """Strip HTML tags and unescape character entities."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def parse_date(date_str: str) -> str:
    """Parse feed date string (ISO 8601 or RFC 822) to ISO 8601 string."""
    if not date_str:
        return datetime.now().isoformat()
    raw = date_str.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat()
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return dt.isoformat()
    except Exception:
        return raw


def _clean_tag(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1].lower()


def parse_feed(content: str | bytes) -> list[FeedEntry]:
    """Parse Atom or RSS XML content and return structured FeedEntry items."""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("latin-1", errors="replace")
    else:
        text = content

    if not text or not text.strip():
        raise FeedParseError("Bo'sh xml oqimi")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FeedParseError(f"XML sintaksis xatosi: {e}") from e

    root_tag = _clean_tag(root)
    entries: list[FeedEntry] = []

    if root_tag == "feed":
        # Atom feed
        for child in root:
            if _clean_tag(child) != "entry":
                continue

            entry_id = ""
            title = ""
            link = ""
            published = ""
            summary = ""
            content_val = ""

            for prop in child:
                ptag = _clean_tag(prop)
                ptext = (prop.text or "").strip()
                if ptag == "id" and not entry_id:
                    entry_id = ptext
                elif ptag == "title" and not title:
                    title = strip_html(ptext)
                elif ptag == "link":
                    href = prop.attrib.get("href", "")
                    rel = prop.attrib.get("rel", "")
                    if href:
                        if rel == "alternate" or not link:
                            link = href
                    elif ptext and not link:
                        link = ptext
                elif ptag in ("updated", "published") and not published:
                    published = parse_date(ptext)
                elif ptag == "summary" and not summary:
                    summary = strip_html(ptext)
                elif ptag == "content" and not content_val:
                    content_val = ptext

            if not content_val and summary:
                content_val = summary
            if not summary and content_val:
                summary = strip_html(content_val)

            if not entry_id:
                entry_id = link or title

            if entry_id or title:
                entries.append(FeedEntry(
                    entry_id=entry_id,
                    title=title,
                    link=link,
                    published_at=published or datetime.now().isoformat(),
                    summary=summary,
                    raw_content=content_val or summary,
                ))

    elif root_tag in ("rss", "rdf") or any(_clean_tag(c) == "channel" for c in root):
        # RSS 2.0 / RDF feed
        channel = root
        for c in root:
            if _clean_tag(c) == "channel":
                channel = c
                break

        for child in channel:
            if _clean_tag(child) != "item":
                continue

            guid = ""
            title = ""
            link = ""
            pub_date = ""
            description = ""
            encoded_content = ""

            for prop in child:
                ptag = _clean_tag(prop)
                ptext = (prop.text or "").strip()
                if ptag in ("guid", "id") and not guid:
                    guid = ptext
                elif ptag == "title" and not title:
                    title = strip_html(ptext)
                elif ptag == "link" and not link:
                    link = ptext or prop.attrib.get("href", "")
                elif ptag in ("pubdate", "date") and not pub_date:
                    pub_date = parse_date(ptext)
                elif ptag == "description" and not description:
                    description = strip_html(ptext)
                elif ptag in ("encoded", "content") and not encoded_content:
                    encoded_content = ptext

            if not encoded_content and description:
                encoded_content = description
            if not description and encoded_content:
                description = strip_html(encoded_content)

            entry_id = guid or link or title
            if entry_id or title:
                entries.append(FeedEntry(
                    entry_id=entry_id,
                    title=title,
                    link=link,
                    published_at=pub_date or datetime.now().isoformat(),
                    summary=description,
                    raw_content=encoded_content or description,
                ))
    else:
        raise FeedParseError(f"Noma'lum feed turi: root tag <{root_tag}>")

    return entries
