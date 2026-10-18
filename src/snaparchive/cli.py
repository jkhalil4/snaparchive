"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .archive import Archive
from .export import scan_inputs
from .inbox import run_inbox
from .ingest import Busy, IngestOptions, ingest, missing_from_archive, save_report, state_lock
from .media import EncodeOptions

log = logging.getLogger("snaparchive")


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from exc


def _mode(value: str) -> int:
    try:
        return int(value, 8)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use an octal mode like 0640") from exc


def _add_archive(p: argparse.ArgumentParser) -> None:
    p.add_argument("--archive", required=True, type=Path, help="folder that holds the archived memories")
    p.add_argument("--state", type=Path, help="where the manifest, reports and logs go (default: <archive>/../.snaparchive)")


def _add_processing(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", action="store_true", help="show what would be added, change nothing")
    p.add_argument("--workers", type=int, default=2, help="memories processed in parallel (default 2)")
    p.add_argument("--since", type=_date, help="ignore memories captured before this date (YYYY-MM-DD)")
    p.add_argument("--crf", type=int, default=18, help="x264 quality for videos with overlays (default 18)")
    p.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    p.add_argument("--no-gps", action="store_true", help="do not write GPS coordinates into files")
    p.add_argument("--no-download", action="store_true", help="never fetch memories over the network")
    p.add_argument("--file-mode", type=_mode, default=0o640, help="permissions for new files (default 0640)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snaparchive",
        description="Keep a local archive of your Snapchat Memories up to date from Snapchat data exports.",
    )
    parser.add_argument("--version", action="version", version=f"snaparchive {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--nice", type=int, default=10, help="lower CPU priority by this much (default 10)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="add new memories from export ZIPs, folders or memories_history files")
    p.add_argument("inputs", nargs="+", type=Path)
    _add_archive(p)
    _add_processing(p)
    p.add_argument("--json", action="store_true", help="print the report as JSON")

    p = sub.add_parser("inbox", help="ingest everything waiting in a drop folder, then file it away (for cron)")
    p.add_argument("--inbox", required=True, type=Path)
    p.add_argument("--exports", required=True, type=Path, help="where processed export files are kept")
    p.add_argument("--min-age", type=float, default=600, help="seconds the inbox must be untouched first (default 600)")
    _add_archive(p)
    _add_processing(p)

    p = sub.add_parser("status", help="summarise the archive: counts, newest memory, empty months")
    _add_archive(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify", help="check an export's memory list against the archive and list what is missing")
    p.add_argument("inputs", nargs="+", type=Path)
    _add_archive(p)
    p.add_argument("--json", action="store_true")
    return parser


def _setup_logging(verbose: bool, state: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if state is not None:
        try:
            (state / "logs").mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(state / "logs" / "snaparchive.log", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            root.addHandler(fh)
        except OSError as exc:
            log.warning("cannot write log file in %s: %s", state, exc)


def _options(args) -> IngestOptions:
    return IngestOptions(
        dry_run=args.dry_run,
        workers=args.workers,
        file_mode=args.file_mode,
        since=args.since,
        downloads=not args.no_download,
        encode=EncodeOptions(crf=args.crf, preset=args.preset, write_gps=not args.no_gps),
    )


def _print_report(report: dict) -> None:
    if report["dry_run"]:
        print(f"\nwould add:        {report['new']}")
    else:
        print(f"\nadded:            {len(report['added'])}")
        print(f"  overlays merged: {sum(1 for r in report['added'] if r['overlay'] == 'merged')}")
    print(f"already archived: {sum(report['already_archived'].values())}  {report['already_archived']}")
    print(f"failed:           {len(report['failed'])}")
    for f in report["failed"][:20]:
        print(f"  - {f['memory']}: {f['error']}")
    for r in report["overlay_problems"][:20]:
        print(f"  overlay not merged: {r['file']}: {r['overlay']}")
    if report["listed_without_media"]:
        print(f"listed in the export but no media found: {len(report['listed_without_media'])}")
        for line in report["listed_without_media"][:20]:
            print(f"  - {line}")
    after = report.get("archive_after")
    if after:
        print(f"archive now:      {after['files']} files, newest {after['newest']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    archive_dir: Path = args.archive.resolve()
    state: Path = (args.state or archive_dir.parent / ".snaparchive").resolve()
    writes = args.command in ("ingest", "inbox") and not getattr(args, "dry_run", False)
    _setup_logging(args.verbose, state if writes else None)
    if args.nice:
        try:
            os.nice(args.nice)
        except OSError:
            pass

    archive = Archive(archive_dir, state)
    if args.command == "status":
        summary = archive.summary()
        summary["manifest_records"] = len(archive.read_manifest())
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"{summary['files']} files ({summary['images']} images, {summary['videos']} videos) in {summary['folder']}")
            print(f"oldest {summary['oldest']}   newest {summary['newest']}")
            print("per year: " + ", ".join(f"{y}: {n}" for y, n in summary["per_year"].items()))
            if summary["empty_months"]:
                print("months with no memories: " + ", ".join(summary["empty_months"]))
            if summary["unrecognised_names"]:
                print(f"{summary['unrecognised_names']} files do not follow the naming scheme")
        return 0

    if args.command == "verify":
        contents = scan_inputs(args.inputs)
        missing = missing_from_archive(archive, contents.entries)
        result = {
            "listed": len(contents.entries),
            "missing": len(missing),
            "missing_memories": [f"{e.taken:%Y-%m-%d %H:%M:%S} {e.kind or '?'}" for e in missing],
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"{result['listed']} memories listed, {result['missing']} not found in the archive")
            for line in result["missing_memories"]:
                print(f"  - {line}")
        return 1 if missing else 0

    try:
        with state_lock(state) if writes else nullcontext():
            if args.command == "ingest":
                report = ingest(args.inputs, archive, _options(args))
                path = save_report(report, state) if not args.dry_run else None
                if args.json:
                    print(json.dumps(report, indent=2, default=str))
                else:
                    _print_report(report)
                    if path:
                        print(f"report:           {path}")
                return 2 if report["failed"] else 0
            if args.command == "inbox":
                summary = run_inbox(args.inbox, args.exports, archive, _options(args), args.min_age)
                print(json.dumps(summary))
                return 2 if summary.get("failed") else 0
    except Busy as exc:
        log.warning("%s", exc)
        if args.command == "inbox":
            print(json.dumps({"status": "busy"}))
            return 0
        return 3
    return 0
