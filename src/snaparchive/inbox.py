"""Drop-folder automation: ingest whatever export files appear in an inbox folder.

Meant to run from cron. Each run either does nothing (empty inbox, or files
still uploading), or ingests everything in the inbox as one batch and then moves
those files into the exports folder, so the raw export is kept as-is.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .archive import Archive
from .ingest import IngestOptions, ingest, save_report

log = logging.getLogger(__name__)

# names browsers and sync clients use while a file is still arriving
PARTIAL_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp", ".filepart")
REJECTED_DIR = "rejected"


def _candidates(inbox: Path) -> list[Path]:
    out = []
    for p in sorted(inbox.iterdir()):
        name = p.name.lower()
        if name.startswith((".", "~")) or p.name == REJECTED_DIR or ".ocTransferId".lower() in name:
            continue
        if name.endswith(PARTIAL_SUFFIXES):
            continue
        if p.is_dir() or name.endswith((".zip", ".json", ".html")):
            out.append(p)
    return out


def _newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_dir():
            for dirpath, _, files in os.walk(p):
                for f in files:
                    try:
                        newest = max(newest, os.stat(os.path.join(dirpath, f)).st_mtime)
                    except OSError:
                        pass
        newest = max(newest, p.stat().st_mtime)
    return newest


def _move(paths: list[Path], dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for p in paths:
        target = dest / p.name
        n = 1
        while target.exists():
            n += 1
            target = dest / f"{p.stem}-{n}{p.suffix}"
        shutil.move(str(p), str(target))
        moved.append(str(target))
    return moved


def run_inbox(inbox: Path, exports: Path, archive: Archive, opts: IngestOptions, min_age: float = 600) -> dict:
    inbox.mkdir(parents=True, exist_ok=True)
    items = _candidates(inbox)
    # Partial uploads (and any other in-flight file) still count as activity.
    activity = [p for p in inbox.iterdir() if not p.name.startswith(".") and p.name != REJECTED_DIR]
    if not items:
        return {"status": "idle"}
    age = time.time() - _newest_mtime(activity)
    if age < min_age:
        return {"status": "waiting", "seconds_since_last_change": int(age), "files": [p.name for p in items]}

    good, bad = [], []
    for p in items:
        if p.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(p) as zf:
                    zf.infolist()
            except (zipfile.BadZipFile, OSError) as exc:
                log.error("%s is not a readable ZIP (%s); moving it to %s/", p.name, exc, REJECTED_DIR)
                bad.append(p)
                continue
        good.append(p)
    rejected = _move(bad, inbox / REJECTED_DIR) if bad else []
    if not good:
        return {"status": "rejected", "rejected": rejected}

    report = ingest(good, archive, opts)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    if opts.dry_run:
        moved = []
    else:
        moved = _move(good, exports / stamp)
    report["moved_to"] = moved
    report["rejected"] = rejected
    path = save_report(report, archive.state_dir)
    return {
        "status": "dry-run" if opts.dry_run else "ingested",
        "added": len(report["added"]),
        "overlays_merged": sum(1 for r in report["added"] if r["overlay"] == "merged"),
        "overlay_problems": len(report["overlay_problems"]),
        "failed": len(report["failed"]),
        "already_archived": sum(report["already_archived"].values()),
        "listed_without_media": len(report["listed_without_media"]),
        "newest": (report.get("archive_after") or report["archive_before"]).get("newest"),
        "exports_folder": str(exports / stamp) if moved else None,
        "rejected": rejected,
        "report": str(path) if path else None,
    }
