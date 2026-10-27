"""Deterministic triage and deduplication clustering for vendor updates."""

from datetime import datetime
import re

_SEMVER_RE = re.compile(r"(?:^|[\s/vV])(\d+)\.(\d+)\.(\d+)(?:[-.]([a-zA-Z0-9.]+))?")

_DEPRECATION_KEYWORDS = (
    "deprecat",
    "shutdown",
    "sunsetting",
    "sunset",
    "end of life",
    "discontinued",
    "retired",
)

_SECURITY_KEYWORDS = (
    "cve-",
    "vulnerability",
    "security fix",
    "security advisory",
    "critical patch",
    "auth bypass",
    "rce",
    "exploit",
)

_BREAKING_KEYWORDS = (
    "breaking change",
    "breaking:",
    "breaking ",
    "backward incompatible",
    "removed in v",
    "migration guide",
)

_NEW_MODEL_KEYWORDS = (
    "new model",
    "introducing gpt",
    "introducing gemini",
    "introducing claude",
    "model release",
    "opus 5",
    "sonnet 4",
    "gemini 2.5",
    "gemini 3",
    "gemini 4",
    "gpt-5",
    "gpt-6",
    "claude 4",
    "argon",
    "astra",
)

_PRICING_KEYWORDS = (
    "price reduction",
    "pricing update",
    "price drop",
    "pricing:",
    "cost per token",
    "token pricing",
    "billing update",
)

_FEATURE_KEYWORDS = (
    "feature:",
    "feat:",
    "support for",
    "new feature",
    "added",
    "introducing",
)


def extract_semver(text: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.search(text)
    if match:
        try:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        except ValueError:
            return None
    return None


def classify_event(event: dict) -> tuple[str, str]:
    """Determine (type, priority) from title, content, product name and SemVer.

    Types: new_model | breaking | security | deprecation | pricing | feature | sdk_patch | other
    Priority: high vs digest
    """
    title = (event.get("title") or "").lower()
    raw = (event.get("raw_content") or event.get("summary") or "").lower()
    full_text = f"{title}\n{raw}"
    product = (event.get("product") or "").lower()
    source_id = (event.get("source_id") or "").lower()

    # 1. Deprecation / shutdown -> always high
    if any(k in full_text for k in _DEPRECATION_KEYWORDS):
        return "deprecation", "high"

    # 2. Security fixes / vulnerabilities -> always high
    if any(k in full_text for k in _SECURITY_KEYWORDS):
        return "security", "high"

    # 3. Breaking changes (explicit keyword or SemVer major bump)
    is_breaking_kw = False
    if (
        "no breaking" not in full_text
        and "without breaking" not in full_text
        and "non-breaking" not in full_text
        and "not breaking" not in full_text
    ):
        is_breaking_kw = any(k in full_text for k in _BREAKING_KEYWORDS)
    semver = extract_semver(title) or extract_semver(raw)
    if is_breaking_kw:
        return "breaking", "high"

    if semver:
        major, minor, patch = semver
        if major >= 2 and minor == 0 and patch == 0:
            return "breaking", "high"

    # 4. New model releases -> high
    if any(k in full_text for k in _NEW_MODEL_KEYWORDS):
        return "new_model", "high"

    # 5. Pricing updates -> high
    if any(k in full_text for k in _PRICING_KEYWORDS):
        return "pricing", "high"

    # 6. SDK patch releases -> digest unless security/breaking (handled above)
    is_sdk = "sdk" in product or "python" in product or "cli" in product or "sdk" in source_id
    if is_sdk and semver:
        _, _, patch = semver
        if patch > 0:
            return "sdk_patch", "digest"

    # 7. Features
    if any(k in full_text for k in _FEATURE_KEYWORDS):
        return "feature", "digest"

    if semver and semver[2] > 0:
        return "sdk_patch", "digest"

    # 8. Fallback
    return "other", "digest"


def _extract_entity_key(title: str, text: str) -> str:
    """Extract model or release key for cluster matching (e.g. gemini-2.5-flash, v1.50.0)."""
    normalized = re.sub(r"[\s\-_]+", "-", f"{title} {text}".lower())
    # Check model names
    models = (
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-3.8-flash",
        "gemini-4-argon",
        "gpt-6-astra",
        "gpt-5",
        "claude-opus-5.5",
        "claude-sonnet-4",
    )
    for m in models:
        if m in normalized:
            return m

    # Check semver
    sv = extract_semver(title)
    if sv:
        return f"v{sv[0]}.{sv[1]}.{sv[2]}"

    return ""


def _parse_time(ts_str: str) -> float:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def cluster_duplicates(events: list[dict]) -> list[dict]:
    """Cluster duplicate events across sources (same vendor + model/version within 24h)."""
    if len(events) <= 1:
        return events

    clusters: list[list[dict]] = []
    for ev in events:
        vendor = (ev.get("vendor") or "").strip().lower()
        key = _extract_entity_key(ev.get("title", ""), ev.get("raw_content", ""))
        t = _parse_time(ev.get("published_at", ""))

        placed = False
        if vendor and key:
            for cluster in clusters:
                first = cluster[0]
                c_vendor = (first.get("vendor") or "").strip().lower()
                c_key = _extract_entity_key(first.get("title", ""), first.get("raw_content", ""))
                c_t = _parse_time(first.get("published_at", ""))

                if vendor == c_vendor and key == c_key:
                    # Check 24 hour window
                    if abs(t - c_t) <= 86400:
                        cluster.append(ev)
                        placed = True
                        break

        if not placed:
            clusters.append([ev])

    merged_events = []
    for cluster in clusters:
        if len(cluster) == 1:
            ev = cluster[0]
            if "links" not in ev:
                ev["links"] = [ev["link"]] if ev.get("link") else []
            merged_events.append(ev)
        else:
            # Merge cluster
            primary = cluster[0]
            all_links = []
            all_products = []
            for item in cluster:
                link = item.get("link")
                if link and link not in all_links:
                    all_links.append(link)
                prod = item.get("product")
                if prod and prod not in all_products:
                    all_products.append(prod)

            merged = dict(primary)
            merged["links"] = all_links
            merged["clustered_sources"] = all_products
            merged["cluster_count"] = len(cluster)
            merged_events.append(merged)

    return merged_events
