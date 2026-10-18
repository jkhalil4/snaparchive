import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from snaparchive.archive import Archive, short_id
from snaparchive.export import build_memories, parse_memories_html, parse_memories_json, scan_inputs
from snaparchive.model import IMAGE, VIDEO, Memory, normalize_kind, parse_location, parse_snap_date

UTC = timezone.utc


class ParseValues(unittest.TestCase):
    def test_dates(self):
        want = datetime(2025, 7, 4, 21, 30, 15, tzinfo=UTC)
        for value in ("2025-07-04 21:30:15 UTC", "2025-07-04T21:30:15Z", "2025-07-04T16:30:15-05:00", 1751664615, "1751664615000"):
            self.assertEqual(parse_snap_date(value), want, value)
        self.assertIsNone(parse_snap_date("yesterday"))
        self.assertIsNone(parse_snap_date(None))

    def test_locations(self):
        self.assertEqual(parse_location("Latitude, Longitude: 39.96, -83.0"), (39.96, -83.0))
        self.assertEqual(parse_location("Latitude, Longitude: 39.96 ± 14.2 meters, -83.0 ± 14.2 meters"), (39.96, -83.0))
        self.assertEqual(parse_location({"Latitude": "1.5", "Longitude": 2}), (1.5, 2.0))
        self.assertEqual(parse_location("Latitude, Longitude: 0.0, 0.0"), (None, None))
        self.assertEqual(parse_location("Latitude, Longitude: 95, 10"), (None, None))
        self.assertEqual(parse_location(""), (None, None))

    def test_kinds(self):
        self.assertEqual(normalize_kind("Video"), VIDEO)
        self.assertEqual(normalize_kind("IMAGE"), IMAGE)
        self.assertEqual(normalize_kind("Photo"), IMAGE)
        self.assertIsNone(normalize_kind("Download"))


class ParseFiles(unittest.TestCase):
    def test_json_rows_and_ids(self):
        mid = "7C41D09E-1C2D-4E5F-8A9B-0C1D2E3F4A5B"
        data = {
            "Saved Media": [
                {
                    "Date": "2025-07-04 21:30:15 UTC",
                    "Media Type": "Video",
                    "Location": "Latitude, Longitude: 40.1, -83.2",
                    "Download Link": f"https://app.snapchat.com/dmd/memories?uid=x&sid={mid.lower()}&mid={mid}&ts=1&sig=abc",
                },
                {"Date": "2026-01-23 10:00:00 UTC", "Media Type": "Image", "Download Link": "N/A"},
            ]
        }
        rows = parse_memories_json(json.dumps(data))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].media_ids, (mid.lower(),))
        self.assertEqual((rows[0].lat, rows[0].lon), (40.1, -83.2))
        self.assertFalse(rows[0].url_is_get)
        self.assertIsNone(rows[1].url)

    def test_html_table(self):
        html = """<table><tr><th>Date</th></tr>
        <tr><td>2025-11-30 00:31:09 UTC</td><td>Video</td><td>Latitude, Longitude: 34.05, -118.25</td>
            <td><a href="#" onclick="downloadMemories('https://cdn.example/x.mp4?a=1', this, true); return false;">Download</a></td></tr>
        <tr><td>2025-11-29 10:00:00 UTC</td><td>Image</td><td>Latitude, Longitude: 0.0, 0.0</td><td>N/A</td></tr>
        </table>"""
        rows = parse_memories_html(html)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].kind, VIDEO)
        self.assertTrue(rows[0].url_is_get)
        self.assertEqual(rows[0].url, "https://cdn.example/x.mp4?a=1")
        self.assertEqual((rows[1].lat, rows[1].url), (None, None))


def _zip(path: Path, members: dict[str, tuple[bytes, datetime]], rows: list[dict]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("json/memories_history.json", json.dumps({"Saved Media": rows}))
        for name, (data, when) in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=when.timetuple()[:6]), data)
    return path


class JoinMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return [
            {"Date": "2026-03-01 12:00:01 UTC", "Media Type": "Image", "Location": "Latitude, Longitude: 10.5, 20.5"},
            {"Date": "2026-03-01 12:00:05 UTC", "Media Type": "Video", "Location": "Latitude, Longitude: 11.5, 21.5"},
            {"Date": "2026-03-02 08:00:00 UTC", "Media Type": "Image", "Location": "Latitude, Longitude: 0.0, 0.0"},
        ]

    def test_join_by_member_time(self):
        a, b = "AAAAAAAA-0000-0000-0000-000000000001", "BBBBBBBB-0000-0000-0000-000000000002"
        z = _zip(self.dir / "mydata~1.zip", {
            f"memories/2026-03-01_{a}-main.jpg": (b"\xff\xd8\xff" + b"0" * 200, datetime(2026, 3, 1, 12, 0, 2)),
            f"memories/2026-03-01_{a}-overlay.png": (b"\x89PNG" + b"0" * 200, datetime(2026, 3, 1, 12, 0, 2)),
            f"memories/2026-03-01_{b}-main.mp4": (b"0000ftypisom" + b"0" * 200, datetime(2026, 3, 1, 12, 0, 4)),
            "chat_media/2026-03-01_b~xyz.jpg": (b"\xff\xd8\xff", datetime(2026, 3, 1)),
        }, self.rows())
        contents = scan_inputs([z])
        memories, unmatched = build_memories(contents)
        self.assertEqual(len(memories), 2)
        img = next(m for m in memories if m.kind == IMAGE)
        vid = next(m for m in memories if m.kind == VIDEO)
        self.assertEqual(img.joined_by, "time")
        self.assertEqual(img.taken, datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC))
        self.assertEqual((img.lat, img.lon), (10.5, 20.5))
        self.assertIsNotNone(img.overlay)
        self.assertEqual(vid.taken, datetime(2026, 3, 1, 12, 0, 5, tzinfo=UTC))
        self.assertEqual(short_id(img), "AAAAAAAA")
        self.assertEqual([u.taken.day for u in unmatched], [2])  # listed, but no media in the files

    def test_join_survives_time_zone_shift(self):
        """An export unzipped on a machine in UTC-5 has file times five hours off."""
        rows = [{"Date": f"2026-03-0{d} 1{d}:00:0{d} UTC", "Media Type": "Image"} for d in range(1, 6)]
        members = {
            f"memories/2026-03-0{d}_{d}AAAAAAA-0000-0000-0000-00000000000{d}-main.jpg":
                (b"\xff\xd8\xff" + b"0" * 100, datetime(2026, 3, d, 10 + d, 0, d) - timedelta(hours=5))
            for d in range(1, 6)
        }
        memories, unmatched = build_memories(scan_inputs([_zip(self.dir / "e.zip", members, rows)]))
        self.assertEqual({m.joined_by for m in memories}, {"time"})
        self.assertEqual(unmatched, [])

    def test_join_by_media_id(self):
        uid = "CCCCCCCC-0000-0000-0000-000000000003"
        rows = [{"Date": "2026-04-01 09:30:00 UTC", "Media Type": "Image",
                 "Download Link": f"https://app.snapchat.com/dmd/memories?uid=u&sid=s&mid={uid}&sig=1"}]
        z = _zip(self.dir / "e.zip", {f"memories/2026-04-01_{uid}-main.jpg": (b"\xff\xd8\xff" + b"0" * 50, datetime(2020, 1, 1))}, rows)
        memories, _ = build_memories(scan_inputs([z]))
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].joined_by, "id")
        self.assertEqual(memories[0].taken.hour, 9)

    def test_same_export_twice_is_not_doubled(self):
        z = _zip(self.dir / "a.zip", {"memories/2026-03-01_DDDDDDDD-0000-0000-0000-000000000004-main.jpg":
                                      (b"\xff\xd8\xff" + b"0" * 50, datetime(2026, 3, 1, 12, 0, 1))}, self.rows())
        extracted = self.dir / "extracted"
        with zipfile.ZipFile(z) as zf:
            zf.extractall(extracted)
        contents = scan_inputs([z, extracted])
        memories, _ = build_memories(contents)
        self.assertEqual(len(contents.entries), 3)
        self.assertEqual(len(memories), 1)


class Dedupe(unittest.TestCase):
    def test_classify(self):
        with TemporaryDirectory() as tmp:
            media = Path(tmp) / "memories"
            media.mkdir()
            for name in ("2025-07-03_120744_Video_5A6F2B19.mp4", "2025-07-04_213015_Video_7C41D09E.mp4",
                         "2025-07-04_213015_Image_11111111.jpg"):
                (media / name).write_bytes(b"x")
            archive = Archive(media, Path(tmp) / ".snaparchive")
            t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)  # noqa: E731
            mems = [
                Memory(t("2025-07-03 12:07:44"), VIDEO, "5a6f2b19-1111-2222-3333-444444444444"),  # id (any case)
                Memory(t("2025-07-04 21:30:16"), VIDEO, "99999999-1111-2222-3333-444444444444"),  # 1 s from a file...
                Memory(t("2025-07-04 21:30:15"), VIDEO, "88888888-1111-2222-3333-444444444444"),  # ...this one is exact
                Memory(t("2026-02-01 00:00:00"), IMAGE, "77777777-1111-2222-3333-444444444444"),
            ]
            self.assertEqual([s for _, s in archive.classify(mems)], ["id", "new", "time", "new"])
            self.assertEqual(archive.newest, t("2025-07-04 21:30:15"))
            name = archive.reserve_name(mems[1], ".mp4")
            self.assertEqual(name, "2025-07-04_213016_Video_99999999.mp4")
            clash = Memory(t("2025-07-03 12:07:44"), VIDEO, "5A6F2B19-0000-0000-0000-000000000000")
            self.assertEqual(archive.reserve_name(clash, ".mp4"), "2025-07-03_120744_Video_5A6F2B19_2.mp4")


if __name__ == "__main__":
    unittest.main()
