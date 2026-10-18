"""End-to-end: fake exports in, finished archive files out. Needs ffmpeg and exiftool."""

import http.server
import io
import json
import os
import threading
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from helpers import exif, frame_pixel, make_jpeg, make_overlay, make_video, snap_row, video_stream_md5, write_export_zip
from snaparchive.archive import Archive
from snaparchive.cli import main as cli_main
from snaparchive.inbox import run_inbox
from snaparchive.ingest import IngestOptions, ingest
from snaparchive.media import EncodeOptions

UTC = timezone.utc
FAST = EncodeOptions(preset="ultrafast")


def opts(**kw) -> IngestOptions:
    return IngestOptions(workers=2, encode=FAST, **kw)


class Ingest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        src = Path(cls.tmp.name) / "src"
        src.mkdir()
        cls.src = src
        cls.photo = make_jpeg(src / "photo.jpg")
        cls.overlay = make_overlay(src / "overlay.png")
        cls.overlay_webp = make_overlay(src / "overlay.webp", as_webp=True)
        cls.video = make_video(src / "video.mp4")
        cls.rotated = make_video(src / "rotated.mp4", size=(1280, 720), rotate=-90)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = TemporaryDirectory()
        self.root = Path(self.case.name)
        self.media = self.root / "SnapArchive" / "memories"
        self.media.mkdir(parents=True)
        self.state = self.root / "SnapArchive" / ".snaparchive"

    def tearDown(self):
        self.case.cleanup()

    def export(self, name="mydata~1.zip"):
        t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)  # noqa: E731
        ids = {
            "old": "5A6F2B19-0000-4000-8000-000000000001",
            "img": "A1B2C3D4-0000-4000-8000-000000000002",
            "imgov": "B1B2C3D4-0000-4000-8000-000000000003",
            "vid": "C1B2C3D4-0000-4000-8000-000000000004",
            "vidov": "D1B2C3D4-0000-4000-8000-000000000005",
            "rot": "E1B2C3D4-0000-4000-8000-000000000006",
        }
        when = {
            "old": t("2026-01-20 12:07:44"),
            "img": t("2026-02-01 10:00:01"),
            "imgov": t("2026-02-01 10:05:09"),
            "vid": t("2026-02-01 10:00:00"),
            "vidov": t("2026-02-02 18:30:41"),
            "rot": t("2026-02-03 07:15:21"),
        }
        files = [
            (f"memories/2026-01-20_{ids['old']}-main.mp4", self.video, when["old"]),
            (f"memories/2026-02-01_{ids['img']}-main.jpg", self.photo, when["img"]),
            (f"memories/2026-02-01_{ids['imgov']}-main.jpg", self.photo, when["imgov"]),
            (f"memories/2026-02-01_{ids['imgov']}-overlay.png", self.overlay_webp, when["imgov"]),  # WebP named .png
            (f"memories/2026-02-01_{ids['vid']}-main.mp4", self.video, when["vid"]),
            (f"memories/2026-02-02_{ids['vidov']}-main.mp4", self.video, when["vidov"]),
            (f"memories/2026-02-02_{ids['vidov']}-overlay.png", self.overlay, when["vidov"]),
            (f"memories/2026-02-03_{ids['rot']}-main.mp4", self.rotated, when["rot"]),
            (f"memories/2026-02-03_{ids['rot']}-overlay.png", self.overlay, when["rot"]),
        ]
        rows = [
            snap_row(when["old"], "Video"),
            snap_row(when["img"], "Image", 39.9612, -82.9988),
            snap_row(when["imgov"], "Image", -33.8688, 151.2093),
            snap_row(when["vid"], "Video", 40.7128, -74.006),
            snap_row(when["vidov"], "Video"),
            snap_row(when["rot"], "Video", 51.5, -0.12),
            snap_row(t("2026-02-04 12:00:00"), "Image"),  # listed but not delivered
        ]
        return write_export_zip(self.root / name, files, rows), ids, when

    def test_full_run(self):
        # the archive already has the 2026-01-20 memory under the old naming scheme
        (self.media / "2026-01-20_120744_Video_5A6F2B19.mp4").write_bytes(b"already here")
        zpath, ids, when = self.export()
        archive = Archive(self.media, self.state)
        report = ingest([zpath], archive, opts())

        self.assertEqual(report["failed"], [])
        self.assertEqual(report["already_archived"], {"id": 1})
        names = sorted(r["file"] for r in report["added"])
        self.assertEqual(names, [
            "2026-02-01_100000_Video_C1B2C3D4.mp4",
            "2026-02-01_100001_Image_A1B2C3D4.jpg",
            "2026-02-01_100509_Image_B1B2C3D4.jpg",
            "2026-02-02_183041_Video_D1B2C3D4.mp4",
            "2026-02-03_071521_Video_E1B2C3D4.mp4",
        ])
        self.assertEqual(report["listed_without_media"], ["2026-02-04 12:00:00 Image"])
        merged = {r["file"] for r in report["added"] if r["overlay"] == "merged"}
        self.assertEqual(len(merged), 3)

        # plain photo: original pixels, UTC date with explicit offset, GPS
        img = self.media / "2026-02-01_100001_Image_A1B2C3D4.jpg"
        tags = exif(img, "DateTimeOriginal", "OffsetTimeOriginal", "GPSLatitude", "GPSLongitude")
        self.assertEqual(tags["DateTimeOriginal"], "2026:02:01 10:00:01")
        self.assertEqual(tags["OffsetTimeOriginal"], "+00:00")
        self.assertAlmostEqual(tags["GPSLatitude"], 39.9612, places=4)
        self.assertAlmostEqual(tags["GPSLongitude"], -82.9988, places=4)
        self.assertEqual(int(os.stat(img).st_mtime), int(when["img"].timestamp()))
        self.assertEqual(os.stat(img).st_mode & 0o777, 0o640)

        # photo + WebP overlay: overlay burned in, southern/eastern hemisphere GPS
        imgov = self.media / "2026-02-01_100509_Image_B1B2C3D4.jpg"
        with Image.open(imgov) as im:
            self.assertEqual(im.size, (720, 1280))
            self.assertGreater(min(im.getpixel((50, 50))), 245)  # white box from the overlay
            self.assertLess(im.getpixel((600, 1100))[0], 80)  # untouched photo colour
        tags = exif(imgov, "GPSLatitude", "GPSLongitude")
        self.assertAlmostEqual(tags["GPSLatitude"], -33.8688, places=4)
        self.assertAlmostEqual(tags["GPSLongitude"], 151.2093, places=4)

        # plain video: stream untouched, Snapchat's own timestamp kept, GPS added
        vid = self.media / "2026-02-01_100000_Video_C1B2C3D4.mp4"
        self.assertEqual(video_stream_md5(vid), video_stream_md5(self.video))
        tags = exif(vid, "GPSLatitude", "GPSLongitude", "CreateDate")
        self.assertAlmostEqual(tags["GPSLatitude"], 40.7128, places=4)
        self.assertAlmostEqual(tags["GPSLongitude"], -74.006, places=4)
        self.assertEqual(tags["CreateDate"], "2026:02:01 10:00:12")

        # video + overlay: re-encoded with the overlay on top
        vidov = self.media / "2026-02-02_183041_Video_D1B2C3D4.mp4"
        self.assertGreater(min(frame_pixel(vidov, 60, 60)), 240)

        # rotated phone video: overlay applied upright, output portrait
        rot = self.media / "2026-02-03_071521_Video_E1B2C3D4.mp4"
        self.assertGreater(min(frame_pixel(rot, 60, 60)), 240)
        with Image.open(rot.with_suffix(".frame.png")) as frame:
            self.assertEqual(frame.size, (720, 1280))

        # manifest has every addition; a second run adds nothing
        manifest = [json.loads(line) for line in (self.state / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(r["file"] for r in manifest), names)
        self.assertTrue(all(len(r["sha256"]) == 64 for r in manifest))
        again = ingest([zpath], Archive(self.media, self.state), opts())
        self.assertEqual(again["added"], [])
        self.assertEqual(sum(again["already_archived"].values()), 6)
        self.assertFalse((self.state / "tmp").exists())

    def test_dry_run_changes_nothing(self):
        zpath, _, _ = self.export()
        report = ingest([zpath], Archive(self.media, self.state), opts(dry_run=True))
        self.assertEqual(report["new"], 6)
        self.assertEqual(list(self.media.iterdir()), [])
        self.assertFalse((self.state / "manifest.jsonl").exists())

    def test_broken_overlay_keeps_the_memory(self):
        when = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        junk = self.root / "junk.png"
        junk.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # PNG signature, garbage body
        uid = "F1B2C3D4-0000-4000-8000-000000000007"
        z = write_export_zip(self.root / "e.zip", [
            (f"memories/2026-05-01_{uid}-main.jpg", self.photo, when),
            (f"memories/2026-05-01_{uid}-overlay.png", junk, when),
        ], [snap_row(when, "Image")])
        report = ingest([z], Archive(self.media, self.state), opts())
        self.assertEqual(len(report["added"]), 1)
        self.assertTrue(report["added"][0]["overlay"].startswith("failed"))
        self.assertEqual(len(report["overlay_problems"]), 1)

    def test_split_export_gets_metadata_from_earlier_part(self):
        """Part 2 of a split export has media but no JSON; the cached JSON from part 1 supplies GPS."""
        when = datetime(2026, 6, 1, 8, 0, 3, tzinfo=UTC)
        uid = "0A0B0C0D-0000-4000-8000-000000000008"
        part1 = write_export_zip(self.root / "mydata~1.zip", [], [snap_row(when, "Image", 48.85, 2.35)])
        part2 = write_export_zip(self.root / "mydata~1-2.zip", [(f"memories/2026-06-01_{uid}-main.jpg", self.photo, when)], [],
                                 json_in_zip=False)
        first = ingest([part1], Archive(self.media, self.state), opts())
        self.assertEqual(first["added"], [])
        second = ingest([part2], Archive(self.media, self.state), opts())
        self.assertEqual(len(second["added"]), 1)
        tags = exif(self.media / second["added"][0]["file"], "GPSLatitude")
        self.assertAlmostEqual(tags["GPSLatitude"], 48.85, places=4)

    def test_cli_status_and_verify(self):
        zpath, _, _ = self.export()
        self.assertEqual(cli_main(["--nice", "0", "ingest", str(zpath), "--archive", str(self.media), "--preset", "ultrafast"]), 0)
        self.assertEqual(cli_main(["status", "--archive", str(self.media)]), 0)
        # the export lists one memory it never delivered, so verify reports it missing
        self.assertEqual(cli_main(["verify", str(zpath), "--archive", str(self.media)]), 1)


class CdnHandler(http.server.BaseHTTPRequestHandler):
    """Pretends to be Snapchat's dmd endpoint plus its CDN."""

    files: dict[str, bytes] = {}
    hits: list[str] = []
    fail_once: set[str] = set()

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        self.hits.append(f"POST {self.path} {body}")
        if self.path.startswith("/dmd/memories") and "mid=" in body:
            mid = body.split("mid=")[1].split("&")[0]
            host = self.headers["Host"]
            self._send(200, f"http://{host}/cdn/{mid}".encode(), "text/plain")
        else:
            self._send(400)

    def do_GET(self):
        self.hits.append(f"GET {self.path}")
        key = self.path.rsplit("/", 1)[-1]
        if key in self.fail_once:
            self.fail_once.discard(key)
            self._send(503)
        elif key in self.files:
            self._send(200, self.files[key])
        else:
            self._send(403)


class Download(unittest.TestCase):
    def test_link_only_export(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = make_jpeg(root / "p.jpg")
            overlay = make_overlay(root / "o.png")
            bundle = io.BytesIO()
            with zipfile.ZipFile(bundle, "w") as zf:
                zf.write(photo, "x-main.jpg")
                zf.write(overlay, "x-overlay.png")
            ids = ["11111111-aaaa-4000-8000-000000000001", "22222222-aaaa-4000-8000-000000000002", "33333333-aaaa-4000-8000-000000000003"]
            CdnHandler.files = {ids[0]: bundle.getvalue(), "direct.jpg": photo.read_bytes()}
            CdnHandler.fail_once = {ids[0]}
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CdnHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            rows = [
                {"Date": "2026-07-01 10:00:00 UTC", "Media Type": "Image", "Location": "Latitude, Longitude: 1.5, 2.5",
                 "Download Link": f"{base}/dmd/memories?uid=me&sid={ids[0]}&mid={ids[0]}&ts=1&sig=s"},
                {"Date": "2026-07-01 11:00:00 UTC", "Media Type": "Image", "Media Download Url": f"{base}/cdn/direct.jpg",
                 "Download Link": f"{base}/dmd/memories?uid=me&mid={ids[1]}"},
                {"Date": "2026-07-01 12:00:00 UTC", "Media Type": "Image",
                 "Download Link": f"{base}/dmd/memories?uid=me&mid={ids[2]}"},  # expired: CDN answers 403
            ]
            (root / "memories_history.json").write_text(json.dumps({"Saved Media": rows}))
            media = root / "archive" / "memories"
            try:
                report = ingest([root / "memories_history.json"], Archive(media, root / "archive" / ".s"), opts())
            finally:
                server.shutdown()
                server.server_close()
            added = sorted(r["file"] for r in report["added"])
            self.assertEqual(added, ["2026-07-01_100000_Image_11111111.jpg", "2026-07-01_110000_Image_22222222.jpg"])
            self.assertEqual([r["overlay"] for r in sorted(report["added"], key=lambda r: r["file"])], ["merged", "none"])
            self.assertEqual(len(report["failed"]), 1)
            self.assertIn("expired", report["failed"][0]["error"])
            self.assertTrue(any(h.startswith("POST /dmd/memories ") and "mid=" + ids[0] in h for h in CdnHandler.hits))


class Inbox(unittest.TestCase):
    def test_waits_then_ingests_and_files_away(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox, exports, media = root / "_inbox", root / "_exports", root / "memories"
            inbox.mkdir()
            photo = make_jpeg(root / "p.jpg")
            when = datetime(2026, 8, 1, 9, 0, 0, tzinfo=UTC)
            uid = "12345678-0000-4000-8000-000000000009"
            z = write_export_zip(inbox / "mydata~123.zip", [(f"memories/2026-08-01_{uid}-main.jpg", photo, when)], [snap_row(when, "Image")])
            (inbox / "mydata~123-2.zip.part").write_bytes(b"still uploading")
            archive = Archive(media, root / ".snaparchive")

            self.assertEqual(run_inbox(inbox, exports, archive, opts(), min_age=600)["status"], "waiting")
            (inbox / "mydata~123-2.zip.part").unlink()
            old = time.time() - 3600
            os.utime(z, (old, old))
            (inbox / "broken.zip").write_bytes(b"PK\x03\x04 not really")
            os.utime(inbox / "broken.zip", (old, old))

            summary = run_inbox(inbox, exports, archive, opts(), min_age=600)
            self.assertEqual(summary["status"], "ingested")
            self.assertEqual(summary["added"], 1)
            self.assertTrue((media / "2026-08-01_090000_Image_12345678.jpg").exists())
            self.assertEqual([p.name for p in inbox.iterdir()], ["rejected"])
            self.assertTrue((inbox / "rejected" / "broken.zip").exists())
            moved = list(exports.glob("*/mydata~123.zip"))
            self.assertEqual(len(moved), 1)
            self.assertEqual(run_inbox(inbox, exports, archive, opts(), min_age=600)["status"], "idle")


if __name__ == "__main__":
    unittest.main()
