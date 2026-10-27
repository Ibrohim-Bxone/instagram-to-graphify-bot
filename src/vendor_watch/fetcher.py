"""Network and fixture fetcher for vendor feeds with conditional HTTP support."""

import logging
from pathlib import Path
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class FetchResult:
    def __init__(
        self,
        status: int,
        content: bytes,
        etag: str = "",
        last_modified: str = "",
        not_modified: bool = False,
        error: str = "",
    ):
        self.status = status
        self.content = content
        self.etag = etag
        self.last_modified = last_modified
        self.not_modified = not_modified
        self.error = error


def fetch_feed(
    source: dict,
    snapshot: dict | None = None,
    fixtures_dir: Path | None = None,
    timeout: int = 20,
    max_retries: int = 2,
) -> FetchResult:
    """Fetch feed content using fixtures or HTTP with ETag/Last-Modified conditionality."""
    source_id = source.get("id", "")

    # 1. Fixtures mode
    if fixtures_dir:
        dir_path = Path(fixtures_dir)
        for ext in (".xml", ".atom", ".rss"):
            target = dir_path / f"{source_id}{ext}"
            if target.is_file():
                return FetchResult(
                    status=200,
                    content=target.read_bytes(),
                    etag=f"fixture-etag-{source_id}",
                    last_modified="Thu, 01 Jan 2026 00:00:00 GMT",
                )
        return FetchResult(
            status=404,
            content=b"",
            error=f"Fixtura topilmadi: {source_id} ({dir_path})",
        )

    # 2. Network HTTP mode
    url = source.get("url", "")
    if not url:
        return FetchResult(status=400, content=b"", error="URL ko'rsatilmagan")

    headers = {
        "User-Agent": "VendorWatch/1.0 (+https://github.com/graphify-ekotizim)",
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    }
    if snapshot:
        if snapshot.get("etag"):
            headers["If-None-Match"] = snapshot["etag"]
        if snapshot.get("last_modified"):
            headers["If-Modified-Since"] = snapshot["last_modified"]

    attempt = 0
    while True:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                etag = resp.headers.get("ETag", "")
                lm = resp.headers.get("Last-Modified", "")
                body = resp.read()
                return FetchResult(
                    status=status,
                    content=body,
                    etag=etag,
                    last_modified=lm,
                    not_modified=False,
                )
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return FetchResult(
                    status=304,
                    content=b"",
                    not_modified=True,
                    etag=snapshot.get("etag", "") if snapshot else "",
                    last_modified=snapshot.get("last_modified", "") if snapshot else "",
                )
            if e.code in (429, 503) and attempt < max_retries:
                attempt += 1
                retry_after = e.headers.get("Retry-After")
                wait_sec = 2.0
                if retry_after:
                    try:
                        wait_sec = min(float(retry_after), 5.0)
                    except ValueError:
                        pass
                log.warning("HTTP %d for %s; retrying in %.1fs", e.code, url, wait_sec)
                time.sleep(wait_sec)
                continue
            return FetchResult(
                status=e.code,
                content=b"",
                error=f"HTTP {e.code}: {e.reason}",
            )
        except (urllib.error.URLError, TimeoutError) as e:
            return FetchResult(
                status=0,
                content=b"",
                error=f"Tarmoq xatosi: {e}",
            )
        except Exception as e:
            return FetchResult(
                status=0,
                content=b"",
                error=f"Kutilmagan xatolik: {e}",
            )
