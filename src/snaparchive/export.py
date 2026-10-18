"""Read Snapchat "Download My Data" exports and turn them into a list of memories.

An export can arrive as one or more ZIP files (large exports are split into
several parts), as an already-extracted folder, or as a bare
memories_history.json/.html. Two layouts exist in the wild:

* Bundled media (2025+): ``memories/<YYYY-MM-DD>_<UUID>-main.<ext>`` plus an
  optional ``-overlay.<ext>`` holding the caption/stickers. The JSON keeps the
  capture time and GPS but no filenames, so rows are joined to files by ID or
  by timestamp.
* Links only (older): every JSON row carries a signed download link and the
  media has to be fetched from Snapchat (see download.py).

Both can occur in the same export, so everything is read into one pool first.
"""

from __future__ import annotations

import bisect
import html.parser
import json
import logging
import os
import re
import struct
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .model import IMAGE, UUID_RE, VIDEO, Blob, Entry, Memory, normalize_kind, parse_location, parse_snap_date

log = logging.getLogger(__name__)

MEDIA_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<id>.+?)(?:-(?P<role>main|overlay))?\.(?P<ext>[A-Za-z0-9]{2,5})$",
    re.IGNORECASE,
)
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
NOT_MEDIA_EXTS = {".html", ".htm", ".json", ".txt", ".css", ".js"}
TIME_TOLERANCE = timedelta(seconds=2)  # DOS timestamps in ZIPs have 2 s resolution


@dataclass
class ExportContents:
    media: list[Blob] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    inputs: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- scanning


def scan_inputs(paths: list[Path]) -> ExportContents:
    contents = ExportContents()
    for path in paths:
        if path.is_dir():
            _scan_dir(path, contents)
        elif path.is_file():
            _scan_file(path, contents)
        else:
            contents.problems.append(f"{path}: not found")
            continue
        contents.inputs.append(path)
    contents.entries = _dedupe_entries(contents.entries)
    return contents


def _scan_dir(root: Path, contents: ExportContents) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__MACOSX")))
        for name in sorted(filenames):
            if not name.startswith("."):
                # keep the root's own name so a folder called "memories" counts as the media dir
                _scan_file(Path(dirpath) / name, contents, rel=os.path.relpath(Path(dirpath) / name, root.parent))


def _scan_file(path: Path, contents: ExportContents, rel: str | None = None) -> None:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        _scan_zip(path, contents)
    elif lower.endswith("memories_history.json") or (rel is None and lower.endswith(".json")):
        contents.entries += parse_memories_json(path.read_bytes(), origin=str(path))
    elif lower.endswith("memories_history.html"):
        contents.entries += parse_memories_html(path.read_text("utf-8", "replace"), origin=str(path))
    elif _is_memory_media(rel or path.name):
        st = path.stat()
        stamp = datetime.fromtimestamp(st.st_mtime, timezone.utc).replace(tzinfo=None)
        contents.media.append(Blob(path, None, st.st_size, stamp, stamp_exact=False))


def _scan_zip(path: Path, contents: ExportContents) -> None:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        contents.problems.append(f"{path.name}: unreadable ZIP ({exc})")
        return
    with zf:
        for info in zf.infolist():
            name = info.filename
            base = name.rsplit("/", 1)[-1]
            if info.is_dir() or not base or base.startswith(".") or name.startswith("__MACOSX"):
                continue
            lower = base.lower()
            if lower == "memories_history.json":
                contents.entries += parse_memories_json(zf.read(info), origin=f"{path.name}:{name}")
            elif lower == "memories_history.html":
                text = zf.read(info).decode("utf-8", "replace")
                contents.entries += parse_memories_html(text, origin=f"{path.name}:{name}")
            elif _is_memory_media(name):
                stamp, exact = zip_member_stamp(info)
                contents.media.append(Blob(path, name, info.file_size, stamp, exact))


def _is_memory_media(name: str) -> bool:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    m = MEDIA_NAME_RE.match(base)
    if not m or f".{m['ext'].lower()}" in NOT_MEDIA_EXTS:
        return False
    in_memories_dir = "memories" in (part.lower() for part in name.replace("\\", "/").split("/")[:-1])
    return bool(m["role"]) or in_memories_dir


def zip_member_stamp(info: zipfile.ZipInfo) -> tuple[datetime | None, bool]:
    """Return the member's modification time as naive UTC, and whether it is exact.

    Info-ZIP "UT" and NTFS extra fields carry a real epoch. Otherwise only the
    DOS wall-clock time is available, which Snapchat writes in UTC.
    """
    extra, i = info.extra, 0
    while i + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, i)
        body = extra[i + 4 : i + 4 + size]
        if tag == 0x5455 and len(body) >= 5 and body[0] & 1:
            (mtime,) = struct.unpack_from("<i", body, 1)
            return datetime.fromtimestamp(mtime, timezone.utc).replace(tzinfo=None), True
        if tag == 0x000A and len(body) >= 32:
            j = 4
            while j + 4 <= len(body):
                attr, attr_size = struct.unpack_from("<HH", body, j)
                if attr == 1 and attr_size >= 24 and j + 12 <= len(body):
                    (ticks,) = struct.unpack_from("<Q", body, j + 4)
                    return datetime(1601, 1, 1) + timedelta(microseconds=ticks // 10), True
                j += 4 + attr_size
        i += 4 + size
    try:
        return datetime(*info.date_time), False
    except ValueError:
        return None, False


# --------------------------------------------------------------------------- metadata files


def parse_memories_json(data: bytes | str, origin: str = "") -> list[Entry]:
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("%s: not valid JSON (%s)", origin, exc)
        return []
    rows = _find_rows(obj)
    entries = [e for e in (_entry_from_row(row, origin) for row in rows) if e]
    if rows and not entries:
        log.warning("%s: %d rows but none had a recognisable date", origin, len(rows))
    return entries


def _find_rows(obj: object) -> list[dict]:
    if isinstance(obj, list):
        dicts = [x for x in obj if isinstance(x, dict)]
        if dicts and any(any(str(k).lower() == "date" for k in d) for d in dicts[:20]):
            return dicts
        return []
    if isinstance(obj, dict):
        for key in ("Saved Media", "saved_media", "Memories", "memories"):
            if isinstance(obj.get(key), list):
                return _find_rows(obj[key])
        for value in obj.values():
            rows = _find_rows(value)
            if rows:
                return rows
    return []


def _usable_url(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def media_ids_from(values: list[object]) -> tuple[str, ...]:
    """Collect memory UUIDs from a row: URL query params first (mid, then sid), then any UUID."""
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        if _usable_url(value):
            query = parse_qs(urlsplit(value).query)
            for param in ("mid", "sid", "media_id", "id"):
                for v in query.get(param, []):
                    if UUID_RE.fullmatch(v.strip()):
                        found.append(v.strip().lower())
        elif UUID_RE.fullmatch(value.strip()):
            found.append(value.strip().lower())
    return tuple(dict.fromkeys(found))


def _entry_from_row(row: dict, origin: str) -> Entry | None:
    keys = {str(k).strip().lower().replace("_", " "): v for k, v in row.items()}
    taken = parse_snap_date(keys.get("date") or keys.get("created") or keys.get("timestamp"))
    if taken is None:
        return None
    lat, lon = parse_location(keys.get("location"))
    direct, link = keys.get("media download url"), keys.get("download link")
    url, is_get = (direct, True) if _usable_url(direct) else (link, False) if _usable_url(link) else (None, False)
    id_values = [link, direct] + [v for k, v in keys.items() if "id" in k.split()]
    return Entry(
        taken=taken,
        kind=normalize_kind(keys.get("media type") or keys.get("type")),
        lat=lat,
        lon=lon,
        media_ids=media_ids_from(id_values),
        url=url.strip() if url else None,
        url_is_get=is_get,
        origin=origin,
    )


class _HistoryTable(html.parser.HTMLParser):
    """memories_history.html: one <tr> per memory with date, type, location and a download cell."""

    _ONCLICK_RE = re.compile(r"downloadMemories\(\s*'([^']+)'(?:\s*,\s*this\s*,\s*(true|false))?")

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = {"cells": [], "url": None, "is_get": False}
        elif tag == "td" and self._row is not None:
            self._in_cell = True
            self._row["cells"].append("")
        elif tag == "a" and self._row is not None:
            onclick = dict(attrs).get("onclick") or ""
            m = self._ONCLICK_RE.search(onclick)
            if m:
                self._row["url"] = m.group(1)
                self._row["is_get"] = m.group(2) == "true"

    def handle_data(self, data):
        if self._in_cell and self._row is not None:
            self._row["cells"][-1] += data

    def handle_endtag(self, tag):
        if tag == "td":
            self._in_cell = False
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def parse_memories_html(text: str, origin: str = "") -> list[Entry]:
    table = _HistoryTable()
    table.feed(text)
    entries = []
    for row in table.rows:
        cells = [c.strip() for c in row["cells"]]
        taken = next((parse_snap_date(c) for c in cells if c.upper().endswith("UTC")), None)
        if taken is None:
            continue
        kind = next((normalize_kind(c) for c in cells if normalize_kind(c) and len(c) < 12), None)
        lat, lon = next((parse_location(c) for c in cells if "latitude" in c.lower()), (None, None))
        url = row["url"] if _usable_url(row["url"]) else None
        entries.append(
            Entry(taken, kind, lat, lon, media_ids_from([url]), url, bool(row["is_get"]), origin)
        )
    return entries


def _dedupe_entries(entries: list[Entry]) -> list[Entry]:
    """The same JSON can be read twice (e.g. a ZIP and its extracted copy); keep one row each."""
    seen: dict[tuple, Entry] = {}
    out = []
    for e in entries:
        ident = (e.key(), e.media_ids, e.lat, e.lon)
        # identical rows from different files are duplicates; identical rows in one file are not
        prior = seen.get(ident)
        if prior is not None and prior.origin != e.origin:
            continue
        seen[ident] = e
        out.append(e)
    return out


# --------------------------------------------------------------------------- joining


def build_memories(contents: ExportContents, extra_entries: list[Entry] | None = None) -> tuple[list[Memory], list[Entry]]:
    """Pair main/overlay files, attach JSON metadata, and return (memories, unmatched_entries).

    Entries from ``extra_entries`` (metadata cached from earlier parts of the same
    export) help with matching but never produce downloads on their own.
    """
    pairs: dict[tuple[str, str], dict[str, Blob]] = defaultdict(dict)
    for blob in contents.media:
        m = MEDIA_NAME_RE.match(blob.name)
        if not m:
            continue
        role = (m["role"] or "main").lower()
        key = (m["date"], m["id"].lower())
        if role in pairs[key]:
            if pairs[key][role].size != blob.size:
                log.warning("two different %s files for %s; keeping %s", role, key[1], pairs[key][role].describe())
            continue
        pairs[key][role] = blob

    memories: list[Memory] = []
    for (day, ident), roles in sorted(pairs.items()):
        main = roles.get("main")
        if main is None:
            contents.problems.append(f"overlay without a main file: {roles['overlay'].describe()}")
            continue
        ext = os.path.splitext(main.name)[1].lower()
        mid = MEDIA_NAME_RE.match(main.name)["id"]  # original case, used for naming
        memories.append(
            Memory(
                taken=datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                kind=VIDEO if ext in VIDEO_EXTS else IMAGE,
                media_id=mid,
                main=main,
                overlay=roles.get("overlay"),
            )
        )

    own = list(contents.entries)
    own_keys = {o.key() for o in own}
    pool = own + [e for e in (extra_entries or []) if e.key() not in own_keys]
    used = _join(memories, pool)

    unmatched = []
    for idx, entry in enumerate(own):
        if idx in used:
            continue
        if entry.url:
            ids = entry.media_ids
            memories.append(
                Memory(entry.taken, entry.kind or IMAGE, ids[0] if ids else None, entry.lat, entry.lon, entry=entry, joined_by="download")
            )
        else:
            unmatched.append(entry)
    memories.sort(key=lambda mem: (mem.taken, mem.kind, mem.media_id or ""))
    return memories, unmatched


def _apply(mem: Memory, entry: Entry, how: str) -> None:
    mem.taken, mem.lat, mem.lon, mem.entry, mem.joined_by = entry.taken, entry.lat, entry.lon, entry, how
    if entry.kind and entry.kind != mem.kind:
        log.debug("%s: export says %s, file says %s; trusting the file", mem.media_id, entry.kind, mem.kind)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _join(memories: list[Memory], entries: list[Entry]) -> set[int]:
    used: set[int] = set()

    # 1. Exact: the row's links name the media UUID.
    by_id: dict[str, int] = {}
    for idx, e in enumerate(entries):
        for mid in e.media_ids:
            by_id.setdefault(mid, idx)
    pending = []
    for mem in memories:
        idx = by_id.get((mem.media_id or "").lower())
        if idx is not None and idx not in used:
            used.add(idx)
            _apply(mem, entries[idx], "id")
        else:
            pending.append(mem)

    # 2. Timestamp: the ZIP records each file's capture time.
    free = sorted((i for i in range(len(entries)) if i not in used), key=lambda i: entries[i].taken)
    times = [_naive(entries[i].taken) for i in free]
    stamped = [m for m in pending if m.main and m.main.stamp]
    offset = _best_offset(stamped, times)
    candidates = []
    for n, mem in enumerate(stamped):
        t = mem.main.stamp + offset  # type: ignore[union-attr]
        lo, hi = bisect.bisect_left(times, t - TIME_TOLERANCE), bisect.bisect_right(times, t + TIME_TOLERANCE)
        for j in range(lo, hi):
            e = entries[free[j]]
            kind_penalty = 0 if (e.kind in (None, mem.kind)) else 1
            candidates.append((kind_penalty, abs((times[j] - t).total_seconds()), n, free[j]))
    taken_mems: set[int] = set()
    for _, _, n, idx in sorted(candidates):
        if n not in taken_mems and idx not in used:
            taken_mems.add(n)
            used.add(idx)
            _apply(stamped[n], entries[idx], "time")
    if offset:
        log.info("export file times are offset by %s from the JSON; corrected", offset)

    # 3. Same day and type, same count: pair them up in chronological order.
    rest = [m for m in pending if m.entry is None]
    groups_m: dict[tuple, list[Memory]] = defaultdict(list)
    for mem in rest:
        groups_m[(mem.taken.date(), mem.kind)].append(mem)
    groups_e: dict[tuple, list[int]] = defaultdict(list)
    for idx, e in enumerate(entries):
        if idx not in used:
            groups_e[(e.taken.date(), e.kind or IMAGE)].append(idx)
    for key, mems in groups_m.items():
        idxs = groups_e.get(key, [])
        if idxs and len(idxs) == len(mems):
            mems.sort(key=lambda m: (m.main.stamp or datetime.min, m.main.name) if m.main else (datetime.min, ""))
            for mem, idx in zip(mems, sorted(idxs, key=lambda i: entries[i].taken)):
                used.add(idx)
                _apply(mem, entries[idx], "date-order")

    # 4. No metadata at all: fall back to the file's own time if it is plausible.
    for mem in memories:
        if mem.entry is None and mem.main and mem.main.stamp:
            guess = (mem.main.stamp + offset).replace(tzinfo=timezone.utc)
            if abs(guess - mem.taken) < timedelta(days=1, hours=14):
                mem.taken = guess
    return used


def _best_offset(stamped: list[Memory], times: list[datetime]) -> timedelta:
    """Find the clock offset (in 15 min steps) that lines file times up with JSON times.

    Handles exports extracted on a machine whose time zone was not UTC.
    """
    if not stamped or not times:
        return timedelta(0)

    def hits(offset: timedelta) -> int:
        count = 0
        for mem in stamped:
            t = mem.main.stamp + offset  # type: ignore[union-attr]
            lo = bisect.bisect_left(times, t - TIME_TOLERANCE)
            count += lo < len(times) and times[lo] <= t + TIME_TOLERANCE
        return count

    best, best_hits = timedelta(0), hits(timedelta(0))
    if best_hits * 2 >= len(stamped):
        return best
    for quarter in range(-14 * 4, 14 * 4 + 1):
        offset = timedelta(minutes=15 * quarter)
        h = hits(offset)
        if h > best_hits:
            best, best_hits = offset, h
    return best if best_hits * 2 >= len(stamped) else timedelta(0)
