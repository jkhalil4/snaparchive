"""The archive folder: what it already holds, file naming, and installing new files.

Files are named ``YYYY-MM-DD_HHMMSS_<Image|Video>_<ID8>.<ext>`` (capture time in
UTC, first 8 characters of Snapchat's memory UUID). Dedupe works from the names
alone; the manifest is an extra record.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import Entry, Memory

log = logging.getLogger(__name__)

ARCHIVE_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{6})_(?P<kind>Image|Video)_(?P<id>[0-9A-Za-z]+)"
    r"(?:_(?P<dup>\d+))?\.(?P<ext>[A-Za-z0-9]+)$"
)
MATCH_WINDOW = timedelta(seconds=2)


@dataclass
class ArchivedFile:
    name: str
    taken: datetime
    kind: str
    short_id: str  # lower-case


def short_id(mem: Memory) -> str:
    """The 8-character ID used in file names, keeping the case Snapchat used."""
    if mem.media_id:
        token = re.sub(r"[^0-9A-Za-z]", "", mem.media_id)[:8]
        if token:
            return token
    seed = f"{mem.taken.isoformat()}|{mem.kind}|{mem.main.describe() if mem.main else ''}|{mem.entry.url if mem.entry else ''}"
    return hashlib.sha1(seed.encode()).hexdigest()[:8].upper()


class Archive:
    def __init__(self, media_dir: Path, state_dir: Path | None = None):
        self.media_dir = media_dir
        self.state_dir = state_dir
        self.files: list[ArchivedFile] = []
        self.unrecognised: list[str] = []
        self._names: set[str] = set()
        self._lock = threading.Lock()
        self.manifest_ids: dict[str, str] = {}
        self.refresh()

    # ------------------------------------------------------------------ index

    def refresh(self) -> None:
        self.files.clear()
        self.unrecognised.clear()
        self._names.clear()
        if self.media_dir.is_dir():
            with os.scandir(self.media_dir) as it:
                for de in it:
                    if de.name.startswith(".") or not de.is_file(follow_symlinks=False):
                        continue
                    self._names.add(de.name)
                    m = ARCHIVE_NAME_RE.match(de.name)
                    if not m:
                        self.unrecognised.append(de.name)
                        continue
                    taken = datetime.strptime(m["date"] + m["time"], "%Y-%m-%d%H%M%S").replace(tzinfo=timezone.utc)
                    self.files.append(ArchivedFile(de.name, taken, m["kind"], m["id"].lower()))
        self.files.sort(key=lambda f: f.taken)
        self.manifest_ids = {
            rec["media_id"].lower(): rec["file"]
            for rec in self.read_manifest()
            if rec.get("media_id") and rec.get("file") in self._names
        }

    @property
    def newest(self) -> datetime | None:
        return self.files[-1].taken if self.files else None

    def classify(self, memories: list[Memory]) -> list[tuple[Memory, str]]:
        """Label each memory "new", or by how it matched an archived file.

        "manifest": full UUID recorded; "id": 8-character ID in a file name;
        "time": same type captured within 2 s. Each file matches one memory at most.
        """
        by_id: dict[str, list[int]] = defaultdict(list)
        by_kind: dict[str, list[int]] = defaultdict(list)  # indices in time order
        for i, f in enumerate(self.files):
            by_id[f.short_id].append(i)
            by_kind[f.kind].append(i)
        kind_times = {k: [self.files[i].taken for i in idxs] for k, idxs in by_kind.items()}
        claimed: set[int] = set()
        result: list[tuple[Memory, str] | None] = [None] * len(memories)

        for n, mem in enumerate(memories):
            if mem.media_id and mem.media_id.lower() in self.manifest_ids:
                result[n] = (mem, "manifest")
                continue
            for i in by_id.get(short_id(mem).lower(), []):
                if i not in claimed:
                    claimed.add(i)
                    result[n] = (mem, "id")
                    break

        # closest pairs first, so an exact-second match wins over a near one
        pairs = []
        for n, mem in enumerate(memories):
            if result[n] is not None:
                continue
            idxs, times = by_kind.get(mem.kind, []), kind_times.get(mem.kind, [])
            lo = bisect.bisect_left(times, mem.taken - MATCH_WINDOW)
            hi = bisect.bisect_right(times, mem.taken + MATCH_WINDOW)
            pairs += [(abs(self.files[i].taken - mem.taken), n, i) for i in idxs[lo:hi] if i not in claimed]
        for _, n, i in sorted(pairs):
            if result[n] is None and i not in claimed:
                claimed.add(i)
                result[n] = (memories[n], "time")
        return [r if r is not None else (memories[n], "new") for n, r in enumerate(result)]

    # ------------------------------------------------------------------ writing

    def reserve_name(self, mem: Memory, ext: str) -> str:
        """Pick a free file name for a memory and hold it until install()."""
        base = f"{mem.taken.astimezone(timezone.utc):%Y-%m-%d_%H%M%S}_{mem.kind}_{short_id(mem)}"
        with self._lock:
            name, n = f"{base}{ext}", 1
            while name in self._names or (self.media_dir / name).exists():
                n += 1
                name = f"{base}_{n}{ext}"
            self._names.add(name)
            return name

    def release_name(self, name: str) -> None:
        with self._lock:
            if not (self.media_dir / name).exists():
                self._names.discard(name)

    def install(self, src: Path, name: str, mem: Memory, mode: int) -> Path:
        """Move a finished file into the archive. Never replaces an existing file."""
        final = self.media_dir / name
        with open(src, "rb") as fh:
            os.fsync(fh.fileno())
        try:
            os.link(src, final)
            os.unlink(src)
        except FileExistsError:
            raise
        except OSError:
            # cross-device or no hard links: copy beside the target, then rename into place
            tmp = final.with_name(f".{final.name}.partial")
            shutil.copyfile(src, tmp)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            if final.exists():
                tmp.unlink()
                raise FileExistsError(final)
            os.rename(tmp, final)
            src.unlink()
        os.chmod(final, mode)
        ts = mem.taken.timestamp()
        os.utime(final, (ts, ts))
        _fsync_dir(self.media_dir)
        return final

    # ------------------------------------------------------------------ manifest

    def _state_file(self, name: str) -> Path | None:
        return self.state_dir / name if self.state_dir else None

    def read_manifest(self) -> list[dict]:
        return _read_jsonl(self._state_file("manifest.jsonl"))

    def record(self, rec: dict) -> None:
        path = self._state_file("manifest.jsonl")
        if path is None:
            return
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def cached_entries(self) -> list[Entry]:
        out = []
        for rec in _read_jsonl(self._state_file("metadata-cache.jsonl")):
            try:
                taken = datetime.fromisoformat(rec["taken"])
            except (KeyError, ValueError):
                continue
            out.append(Entry(taken, rec.get("kind"), rec.get("lat"), rec.get("lon"), tuple(rec.get("ids", ())), origin="cache"))
        return out

    def cache_entries(self, entries: list[Entry]) -> None:
        """Remember export metadata so later parts of a split export still get dates and GPS."""
        path = self._state_file("metadata-cache.jsonl")
        if path is None or not entries:
            return
        known = {(e.key(), e.media_ids) for e in self.cached_entries()}
        fresh = [e for e in entries if e.origin != "cache" and (e.key(), e.media_ids) not in known]
        if not fresh:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for e in fresh:
                rec = {"taken": e.taken.isoformat(), "kind": e.kind, "lat": e.lat, "lon": e.lon, "ids": list(e.media_ids)}
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    # ------------------------------------------------------------------ reporting

    def summary(self) -> dict:
        kinds = Counter(f.kind for f in self.files)
        months = Counter(f.taken.strftime("%Y-%m") for f in self.files)
        gaps = []
        if self.files:
            y, m = self.files[0].taken.year, self.files[0].taken.month
            end = (self.files[-1].taken.year, self.files[-1].taken.month)
            while (y, m) <= end:
                if not months.get(f"{y:04d}-{m:02d}"):
                    gaps.append(f"{y:04d}-{m:02d}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return {
            "folder": str(self.media_dir),
            "files": len(self.files),
            "images": kinds.get("Image", 0),
            "videos": kinds.get("Video", 0),
            "unrecognised_names": len(self.unrecognised),
            "oldest": self.files[0].taken.isoformat() if self.files else None,
            "newest": self.files[-1].taken.isoformat() if self.files else None,
            "per_year": dict(sorted(Counter(f.taken.strftime("%Y") for f in self.files).items())),
            "empty_months": gaps,
        }


def _read_jsonl(path: Path | None) -> list[dict]:
    if path is None or not path.is_file():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    log.warning("%s: skipping a damaged line", path)
    return out


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
