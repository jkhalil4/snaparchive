"""Bring an export into the archive: work out what is new, render it, install it, report."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import shutil
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .archive import Archive
from .download import DownloadError, fetch
from .export import build_memories, scan_inputs
from .media import EncodeOptions, render
from .model import Memory

log = logging.getLogger(__name__)


@dataclass
class IngestOptions:
    dry_run: bool = False
    workers: int = 2
    file_mode: int = 0o640
    since: datetime | None = None
    downloads: bool = True
    encode: EncodeOptions = field(default_factory=EncodeOptions)


class Busy(Exception):
    pass


@contextmanager
def state_lock(state_dir: Path):
    """One run at a time per archive (cron and a manual run must not collide)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy(f"another snaparchive run holds {state_dir / 'lock'}") from exc
        yield


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan(inputs: list[Path], archive: Archive, since: datetime | None = None) -> dict:
    """Read the export and sort every memory into already-archived or new. Changes nothing."""
    contents = scan_inputs(inputs)
    memories, unmatched = build_memories(contents, extra_entries=archive.cached_entries())
    classified = archive.classify(memories)
    if since:
        classified = [(m, s if m.taken >= since else "before-since") for m, s in classified]
    return {
        "contents": contents,
        "memories": memories,
        "classified": classified,
        "new": [m for m, s in classified if s == "new"],
        "unmatched": missing_from_archive(archive, unmatched),
    }


def missing_from_archive(archive: Archive, entries: list) -> list:
    """Metadata rows (no media at hand) that the archive has no file for."""
    probes = [Memory(e.taken, e.kind or "Image", e.media_ids[0] if e.media_ids else None) for e in entries]
    return [e for e, (_, status) in zip(entries, archive.classify(probes)) if status == "new"]


def ingest(inputs: list[Path], archive: Archive, opts: IngestOptions) -> dict:
    started = datetime.now(timezone.utc)
    before = archive.summary()
    p = plan(inputs, archive, opts.since)
    contents, new = p["contents"], p["new"]
    status_counts = Counter(s for _, s in p["classified"])
    joined = Counter(m.joined_by for m in p["memories"])

    log.info(
        "export: %d media files, %d metadata rows -> %d memories (%s)",
        len(contents.media),
        len(contents.entries),
        len(p["memories"]),
        ", ".join(f"{k}: {v}" for k, v in sorted(joined.items())),
    )
    log.info(
        "archive: %d files, newest %s; already archived: %d; new: %d",
        before["files"],
        before["newest"],
        sum(v for k, v in status_counts.items() if k not in ("new", "before-since")),
        len(new),
    )
    for problem in contents.problems:
        log.warning(problem)

    report = {
        "tool": f"snaparchive {__version__}",
        "started": started.isoformat(),
        "dry_run": opts.dry_run,
        "inputs": [str(i) for i in inputs],
        "archive_before": {k: before[k] for k in ("folder", "files", "newest")},
        "export": {
            "media_files": len(contents.media),
            "metadata_rows": len(contents.entries),
            "memories": len(p["memories"]),
            "metadata_joined_by": dict(joined),
        },
        "already_archived": {k: v for k, v in status_counts.items() if k != "new"},
        "new": len(new),
        "added": [],
        "overlay_problems": [],
        "failed": [],
        "listed_without_media": [f"{e.taken:%Y-%m-%d %H:%M:%S} {e.kind or '?'}" for e in p["unmatched"]],
        "problems": contents.problems,
    }
    if p["unmatched"]:
        log.warning(
            "%d memories are listed in the export metadata but have no media in these files "
            "(another part of a split export, or Snapchat left them out)",
            len(p["unmatched"]),
        )
    if not opts.downloads:
        skipped = [m for m in new if m.needs_download]
        new = [m for m in new if not m.needs_download]
        report["failed"] += [{"memory": m.label(), "error": "needs download (--no-download)"} for m in skipped]

    if archive.state_dir and not opts.dry_run:
        archive.cache_entries(contents.entries)  # even with nothing new: later parts may need it
    if opts.dry_run or not new:
        for mem in new:
            log.info("would add %s%s", mem.label(), " (+overlay)" if mem.overlay else "")
        report["finished"] = datetime.now(timezone.utc).isoformat()
        return report

    archive.media_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = (archive.state_dir or archive.media_dir.parent) / "tmp"
    shutil.rmtree(tmp_root, ignore_errors=True)  # leftovers from an interrupted run
    tmp_root.mkdir(parents=True, exist_ok=True)

    done = 0
    lock = threading.Lock()

    def work(mem: Memory) -> dict:
        workdir = Path(tempfile.mkdtemp(dir=tmp_root))
        name = None
        try:
            if mem.main is not None:
                main = workdir / "main.bin"
                mem.main.copy_to(main)
                overlay = None
                if mem.overlay is not None:
                    overlay = workdir / "overlay.bin"
                    mem.overlay.copy_to(overlay)
            else:
                main, overlay = fetch(mem.entry, workdir)  # type: ignore[arg-type]
            out = render(mem, main, overlay, workdir, opts.encode)
            name = archive.reserve_name(mem, out.ext)
            digest = _sha256(out.path)
            size = out.path.stat().st_size
            final = archive.install(out.path, name, mem, opts.file_mode)
            archive.record(
                {
                    "file": final.name,
                    "media_id": mem.media_id,
                    "taken": mem.taken.isoformat(),
                    "kind": mem.kind,
                    "lat": mem.lat,
                    "lon": mem.lon,
                    "overlay": out.overlay,
                    "metadata_joined_by": mem.joined_by,
                    "source": mem.main.describe() if mem.main else "download",
                    "sha256": digest,
                    "size": size,
                    "added": datetime.now(timezone.utc).isoformat(),
                    "tool": __version__,
                }
            )
            return {"file": final.name, "overlay": out.overlay}
        except Exception:
            if name:
                archive.release_name(name)
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        futures = {pool.submit(work, mem): mem for mem in new}
        for fut in as_completed(futures):
            mem = futures[fut]
            with lock:
                done += 1
                prefix = f"[{done}/{len(new)}]"
            try:
                res = fut.result()
            except DownloadError as exc:
                log.error("%s %s: download failed: %s", prefix, mem.label(), exc)
                report["failed"].append({"memory": mem.label(), "error": f"download: {exc}"})
                continue
            except Exception as exc:  # noqa: BLE001 - report and keep going
                log.error("%s %s: %s", prefix, mem.label(), exc)
                report["failed"].append({"memory": mem.label(), "error": str(exc)})
                continue
            note = {"merged": " (overlay merged)", "none": ""}.get(res["overlay"], f" (overlay {res['overlay']})")
            log.info("%s added %s%s", prefix, res["file"], note)
            report["added"].append(res)
            if res["overlay"] not in ("merged", "none"):
                report["overlay_problems"].append(res)

    shutil.rmtree(tmp_root, ignore_errors=True)
    report["added"].sort(key=lambda r: r["file"])
    report["finished"] = datetime.now(timezone.utc).isoformat()
    archive.refresh()
    report["archive_after"] = {k: archive.summary()[k] for k in ("files", "newest")}
    log.info(
        "done: %d added (%d with overlays merged), %d failed",
        len(report["added"]),
        sum(1 for r in report["added"] if r["overlay"] == "merged"),
        len(report["failed"]),
    )
    return report


def save_report(report: dict, state_dir: Path | None) -> Path | None:
    if state_dir is None:
        return None
    reports = state_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = reports / f"{stamp}{'-dry-run' if report.get('dry_run') else ''}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return path
