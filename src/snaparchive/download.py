"""Download memories from exports that only list download links.

"Media Download Url" links are fetched directly. "Download Link" (dmd) links
need the query string POSTed first, which returns a CDN URL. The CDN serves the
media, or a ZIP with -main and -overlay files. Links expire a few days after the
export is created.
"""

from __future__ import annotations

import http.client
import logging
import random
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .media import sniff
from .model import Entry

log = logging.getLogger(__name__)

USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) snaparchive/{__version__}"
GONE = {400, 401, 403, 404, 410}


class DownloadError(Exception):
    pass


class LinkExpired(DownloadError):
    pass


class Truncated(DownloadError):
    pass


def _sleep(attempt: int, retry_after: str | None) -> None:
    try:
        delay = float(retry_after) if retry_after else 0.0
    except ValueError:
        delay = 0.0
    time.sleep(max(delay, min(60.0, 2.0**attempt) + random.uniform(0, 1)))


def _open(req: urllib.request.Request, attempts: int, timeout: float):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in GONE:
                raise LinkExpired(f"HTTP {exc.code} (link expired or memory unavailable)") from exc
            if exc.code != 429 and exc.code < 500:
                raise DownloadError(f"HTTP {exc.code}") from exc
            last = exc
            _sleep(attempt, exc.headers.get("Retry-After") if exc.headers else None)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            last = exc
            _sleep(attempt, None)
    raise DownloadError(f"gave up after {attempts} attempts: {last}")


def resolve_cdn_url(link: str, attempts: int = 5, timeout: float = 60) -> str:
    """Exchange a dmd "Download Link" for the CDN URL that serves the media."""
    base, _, query = link.partition("?")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    tries = [(base, query.encode()), (link, b"")]  # both forms have been used by Snapchat
    error: Exception | None = None
    for url, body in tries:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with _open(req, attempts, timeout) as resp:
                text = resp.read(64 * 1024).decode("utf-8", "replace").strip()
        except DownloadError as exc:
            error = exc
            continue
        if text.startswith(("http://", "https://")):
            return text
        error = DownloadError(f"unexpected answer from Snapchat: {text[:80]!r}")
    raise error or DownloadError("no CDN URL")


def download_file(url: str, dest: Path, attempts: int = 5, timeout: float = 120) -> None:
    """GET url into dest. HTTP errors are retried inside _open; a dropped transfer restarts here."""
    restarts = 3
    for attempt in range(restarts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _open(req, attempts, timeout) as resp, open(dest, "wb") as out:
                expected = resp.headers.get("Content-Length")
                shutil.copyfileobj(resp, out, 1 << 20)
            size = dest.stat().st_size
            if expected is not None and size != int(expected):
                raise Truncated(f"truncated download ({size} of {expected} bytes)")
            if size < 100:
                raise LinkExpired("server returned an empty file (link expired?)")
            return
        except (Truncated, OSError, http.client.HTTPException) as exc:
            if attempt == restarts - 1:
                raise DownloadError(str(exc)) from exc
            _sleep(attempt, None)


def fetch(entry: Entry, workdir: Path) -> tuple[Path, Path | None]:
    """Download one memory into workdir and return (main, overlay)."""
    if not entry.url:
        raise DownloadError("no download link")
    url = entry.url if entry.url_is_get else resolve_cdn_url(entry.url)
    raw = workdir / "download.bin"
    download_file(url, raw)
    if sniff(raw) != "zip":
        return raw, None

    main = overlay = None
    with zipfile.ZipFile(raw) as zf:
        for info in zf.infolist():
            name = info.filename.rsplit("/", 1)[-1].lower()
            if info.is_dir() or name.startswith("."):
                continue
            role = "overlay" if "-overlay" in name else "main" if "-main" in name or main is None else None
            if role is None:
                continue
            target = workdir / f"dl-{role}.bin"  # never trust names from the archive
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if role == "main":
                main = target
            else:
                overlay = target
    raw.unlink()
    if main is None:
        raise DownloadError("downloaded ZIP has no main media file")
    return main, overlay
