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

_CUSTOMER_STORY_KEYWORDS = (
    "is transforming",
    "are transforming",
    "transforming the",
    "customer story",
    "customer stories",
    "case study",
    "case studies",
    "partner post",
    "partner posts",
    "partner story",
    "partner stories",
    "partner spotlight",
    "customer spotlight",
    "success story",
    "success stories",
)

_CUSTOMER_STORY_RE = re.compile(
    r"\bhow\s+[a-zA-Z0-9_\-.]+\s+(?:uses|is using|built|leverages|powers|accelerates|scales)\b",
    re.IGNORECASE,
)

_MODEL_NAME_RE = re.compile(
    r"\b(?:gpt-[0-9a-z.\-]+|gemini-[0-9a-z.\-]+|gemini\s+[0-9.]+|claude-[0-9a-z.\-]+|claude\s+[0-9.]+|opus\s+[0-9.]+|sonnet\s+[0-9.]+|haiku\s+[0-9.]+|argon|astra)\b",
    re.IGNORECASE,
)

_MODEL_RELEASE_WORDING = (
    "introducing",
    "announcing",
    "announces",
    "release",
    "releases",
    "released",
    "releasing",
    "model release",
    "new model",
    "foundation model",
    "frontier model",
    "generally available",
    "now available",
    "launches",
    "launched",
    "launching",
    "presents",
    "unveils",
    "unveiled",
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

_PRERELEASE_KEYWORDS = (
    "-alpha",
    "-beta",
    "-rc",
    ".alpha",
    ".beta",
    ".rc",
    "prerelease",
    "pre-release",
)

SEVERITY_ORDER = {
    "security": 100,
    "breaking": 90,
    "account_change": 85,
    "deprecation": 80,
    "new_model": 70,
    "pricing": 60,
    "feature": 50,
    "sdk_patch": 40,
    "other": 30,
    "prerelease": 20,
}


def is_prerelease(
    title: str,
    text: str = "",
    entry_id: str = "",
    is_prerelease_flag: bool = False,
) -> bool:
    """Check if title, tag or GitHub flag indicates an alpha/beta/rc/preview pre-release.

    Decide prerelease from tag, title, or GitHub prerelease flag, never from body text.
    """
    if is_prerelease_flag:
        return True
    target = f"{title} {entry_id}".lower()
    match = _SEMVER_RE.search(title) or _SEMVER_RE.search(entry_id)
    if match and match.group(4):
        tag = match.group(4).lower()
        if any(w in tag for w in ("alpha", "beta", "rc", "preview", "canary", "dev")):
            return True
    if any(k in target for k in ("-alpha", "-beta", "-rc", ".alpha", ".beta", ".rc")):
        return True
    return False


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
    entry_id = (event.get("entry_id") or "").lower()
    raw = (event.get("raw_content") or event.get("summary") or "").lower()
    full_text = f"{title}\n{raw}"
    product = (event.get("product") or "").lower()
    source_id = (event.get("source_id") or "").lower()

    # Pre-releases are determined from title, entry_id, or GitHub prerelease flag (never from body text)
    prerelease_flag = (
        event.get("prerelease") is True
        or is_prerelease(
            title,
            entry_id=entry_id,
            is_prerelease_flag=bool(event.get("prerelease")),
        )
    )

    # Pre-releases are noise unless security fixes / vulnerabilities
    if prerelease_flag:
        if any(k in full_text for k in _SECURITY_KEYWORDS):
            return "security", "high"
        return "prerelease", "ignore"

    # 1. Deprecation / shutdown -> always high for stable releases
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

    if semver and not prerelease_flag:
        major, minor, patch = semver
        if major >= 2 and minor == 0 and patch == 0:
            return "breaking", "high"

    # Pre-releases are noise unless security or breaking (handled above)
    if prerelease_flag:
        return "prerelease", "ignore"

    # Customer stories, case studies and partner posts ("is transforming", "how X uses", "customer story") are at most digest
    is_customer_story = any(k in full_text for k in _CUSTOMER_STORY_KEYWORDS) or bool(_CUSTOMER_STORY_RE.search(full_text))

    # 4. New model releases -> high only when NOT a customer story and item announces a model release (model name/id plus release wording)
    has_model_name = bool(_MODEL_NAME_RE.search(full_text))
    has_release_wording = any(k in full_text for k in _MODEL_RELEASE_WORDING)

    if not is_customer_story and has_model_name and has_release_wording:
        return "new_model", "high"

    # 5. Pricing updates -> high (unless customer story)
    if not is_customer_story and any(k in full_text for k in _PRICING_KEYWORDS):
        return "pricing", "high"

    # Customer stories, case studies, and partner posts are at most digest
    if is_customer_story:
        if any(k in full_text for k in _FEATURE_KEYWORDS):
            return "feature", "digest"
        return "other", "digest"

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


def _extract_entity_key(title: str, text: str, product: str = "") -> str:
    """Extract model or release key for cluster matching (e.g. gemini-2.5-flash, or product + v1.50.0)."""
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

    # Check semver (must include product in the key so different products don't accidentally merge)
    sv = extract_semver(title) or extract_semver(text)
    if sv:
        prod_clean = re.sub(r"[\s\-_]+", "-", product.strip().lower())
        if prod_clean:
            return f"{prod_clean}:v{sv[0]}.{sv[1]}.{sv[2]}"
        return f"v{sv[0]}.{sv[1]}.{sv[2]}"

    return ""


def _parse_time(ts_str: str) -> float:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def cluster_duplicates(events: list[dict]) -> list[dict]:
    """Cluster duplicate events across sources (same vendor + model/product-version within 24h)."""
    if len(events) <= 1:
        return events

    clusters: list[list[dict]] = []
    for ev in events:
        vendor = (ev.get("vendor") or "").strip().lower()
        key = _extract_entity_key(ev.get("title", ""), ev.get("raw_content", ""), ev.get("product", ""))
        t = _parse_time(ev.get("published_at", ""))

        placed = False
        if vendor and key:
            for cluster in clusters:
                first = cluster[0]
                c_vendor = (first.get("vendor") or "").strip().lower()
                c_key = _extract_entity_key(first.get("title", ""), first.get("raw_content", ""), first.get("product", ""))
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
            if "member_ids" not in ev and ev.get("id"):
                ev["member_ids"] = [ev["id"]]
            merged_events.append(ev)
        else:
            # Merge cluster: keep all member content and the maximum severity
            primary = cluster[0]
            all_links = []
            all_products = []
            all_contents = []
            best_type = primary.get("triage_type") or "other"
            best_prio = primary.get("priority") or "digest"
            if not primary.get("triage_type") or not primary.get("priority"):
                best_type, best_prio = classify_event(primary)
            max_severity = SEVERITY_ORDER.get(best_type, 10)

            for item in cluster:
                link = item.get("link")
                if link and link not in all_links:
                    all_links.append(link)
                for l in item.get("links", []):
                    if l and l not in all_links:
                        all_links.append(l)

                prod = item.get("product")
                if prod and prod not in all_products:
                    all_products.append(prod)

                content = item.get("raw_content") or item.get("summary") or ""
                if content and content not in all_contents:
                    all_contents.append(content)

                itype = item.get("triage_type")
                iprio = item.get("priority")
                if not itype or not iprio:
                    itype, iprio = classify_event(item)
                rank = SEVERITY_ORDER.get(itype, 10)
                if rank > max_severity:
                    max_severity = rank
                    best_type = itype
                    best_prio = iprio

            merged = dict(primary)
            merged["member_ids"] = [item["id"] for item in cluster if item.get("id")]
            merged["links"] = all_links
            merged["clustered_sources"] = all_products
            merged["cluster_count"] = len(cluster)
            merged["raw_content"] = "\n\n---\n\n".join(all_contents) if all_contents else primary.get("raw_content", "")
            merged["triage_type"] = best_type
            merged["priority"] = best_prio
            merged_events.append(merged)

    return merged_events
