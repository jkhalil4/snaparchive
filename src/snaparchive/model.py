"""Plain data types shared by the export reader, the archive and the processor."""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

IMAGE = "Image"
VIDEO = "Video"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def normalize_kind(value: object) -> str | None:
    """Map Snapchat's media type strings ("Image", "VIDEO", "Photo", ...) to IMAGE/VIDEO."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("vid"):
        return VIDEO
    if v.startswith(("im", "photo", "pic")):
        return IMAGE
    return None


def parse_snap_date(value: object) -> datetime | None:
    """Parse "2025-07-04 21:30:15 UTC", ISO 8601, or epoch seconds/milliseconds into aware UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        n = float(value)
        if n > 1e11:  # milliseconds
            n /= 1000
        try:
            return datetime.fromtimestamp(n, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.upper().endswith(" UTC"):
        s = s[:-4]
    s = s.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_location(value: object) -> tuple[float | None, float | None]:
    """Parse "Latitude, Longitude: 40.1, -83.0" into (lat, lon). Snapchat's 0.0, 0.0 means no fix."""
    lat = lon = None
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        lat, lon = lowered.get("latitude"), lowered.get("longitude")
    elif isinstance(value, str):
        text = value.split(":", 1)[1] if ":" in value else value
        parts = text.split(",")
        if len(parts) >= 2:
            first = _NUMBER_RE.search(parts[0])
            second = _NUMBER_RE.search(parts[1])
            if first and second:
                lat, lon = first.group(), second.group()
    try:
        lat_f, lon_f = float(lat), float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180) or (lat_f == 0 and lon_f == 0):
        return None, None
    return lat_f, lon_f


@dataclass(frozen=True)
class Blob:
    """One file of an export: a member of a ZIP archive, or a plain file on disk."""

    path: Path  # the ZIP file, or the plain file itself
    member: str | None  # member name inside the ZIP; None for plain files
    size: int
    stamp: datetime | None  # naive UTC wall-clock recorded for the file, if any
    stamp_exact: bool = False  # True when stamp came from a real epoch, not a DOS time

    @property
    def name(self) -> str:
        return (self.member or self.path.name).rsplit("/", 1)[-1]

    def describe(self) -> str:
        return f"{self.path.name}:{self.member}" if self.member else str(self.path)

    def copy_to(self, dest: Path) -> None:
        if self.member is None:
            shutil.copyfile(self.path, dest)
            return
        with zipfile.ZipFile(self.path) as zf, zf.open(self.member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)


@dataclass
class Entry:
    """One row of memories_history.json / .html."""

    taken: datetime  # aware UTC
    kind: str | None
    lat: float | None = None
    lon: float | None = None
    media_ids: tuple[str, ...] = ()  # lower-case UUIDs found in the row (mid/sid params, ...)
    url: str | None = None
    url_is_get: bool = False
    origin: str = ""

    def key(self) -> tuple[str, str]:
        return (self.taken.strftime("%Y-%m-%dT%H:%M:%S"), self.kind or "")


@dataclass
class Memory:
    """A memory to archive: its metadata plus where its media comes from."""

    taken: datetime  # aware UTC
    kind: str
    media_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    main: Blob | None = None
    overlay: Blob | None = None
    entry: Entry | None = None
    joined_by: str = "none"  # how metadata was matched to media: id, time, date-order, none

    @property
    def needs_download(self) -> bool:
        return self.main is None

    def label(self) -> str:
        return f"{self.taken:%Y-%m-%d %H:%M:%S} {self.kind}" + (f" {self.media_id[:8]}" if self.media_id else "")
