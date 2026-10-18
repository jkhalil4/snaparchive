# snaparchive

Keep a local archive of your Snapchat Memories up to date. Captions, stickers
and drawings get merged back in, and the capture date and GPS are written into
each file. Nothing is ever added twice.

Point it at a Snapchat data export and an archive folder. It works out which
memories the archive is missing and adds only those. Run it again on a newer
export and it picks up where it left off.

## Why

- Snapchat caps free Memories storage at 5 GB. From January 2027, Memories that
  are older than a year and fall outside your oldest 5 GB get archived: they
  show as thumbnails until you pay or download them
  ([Snapchat Support](https://help.snapchat.com/hc/en-us/articles/41291271694228-How-do-I-manage-my-Memories-storage)).
- The data export is not a usable photo library on its own. Overlays come as
  separate transparent images. The capture time and location are only in a
  JSON file, so a photo app shows everything as taken on the day you
  downloaded it, with no captions.

## What it does

- **Reads exports as they arrive.** It takes ZIPs (no unzipping needed),
  exports split over several ZIPs, extracted folders, and bare
  `memories_history.json` or `.html` files. It handles both export styles:
  media bundled in the ZIP, and older exports that carry only download links.
- **Incremental and idempotent.** It knows what the archive holds from the
  file names, by Snapchat's media ID. Files named some other way are matched
  by capture time instead. Feeding it the same or an overlapping export again
  adds nothing.
- **Merges overlays.**
  - Photos: alpha-composited, saved as JPEG at quality 95, 4:4:4.
  - Videos: composited with ffmpeg in RGB, so overlay colours stay exact on
    Snapchat's full-range footage. It accounts for rotation, encodes H.264 at
    CRF 18 and copies the audio. Overlays delivered as WebP disguised as
    `.png` are handled.
- **Leaves originals alone.** A memory without an overlay is kept byte for
  byte. Videos keep their exact stream, and only metadata is added.
- **Writes metadata.** It sets the capture time (UTC, with an explicit
  `+00:00` offset on photos), GPS where Snapchat recorded it (EXIF for photos,
  QuickTime for videos), and the file modification time.
- **Never loses a memory to a broken overlay.** The original is archived
  instead, and the problem is reported.
- **Safe to interrupt.** Files are fully written and synced, then hard-linked
  into place. Existing files are never replaced. Only one run happens at a time.
- **Keeps records.** Each run writes a JSON report and appends to a manifest
  holding the SHA-256 of every file added.
- **Drop-folder mode for cron.** For Nextcloud All-in-One there is a ready-made
  wrapper that also rescans Nextcloud, reindexes the Memories app and sends a
  notification. See [deploy/nextcloud-aio](deploy/nextcloud-aio/README.md).

## Get your export

1. Log in at [accounts.snapchat.com](https://accounts.snapchat.com) and open **My Data**.
   In the app, go to Settings (gear on your profile), then **My Data**.
2. Turn on **Export your Memories**. Turn on **Export JSON Files** too if it is
   offered; it carries dates and locations.
3. Click **Request Only Memories** and pick a date range. Start a day or two
   before the newest memory you already have; overlap is harmless.
4. Submit. Snapchat emails a link when the export is ready, which can take
   hours or days. It also appears under **See exports**. Download **every** ZIP:
   large exports are split.

## Run it

### Docker (includes ffmpeg and exiftool)

```sh
docker build -t snaparchive .

# what would be added?
docker run --rm -v /path/to/archive:/archive -v /path/to/downloads:/in:ro \
    snaparchive ingest /in --archive /archive/memories --dry-run

# do it
docker run --rm -v /path/to/archive:/archive -v /path/to/downloads:/in:ro \
    snaparchive ingest /in --archive /archive/memories
```

Add `--user UID:GID` so new files get the owner you want.

### Python

Requires Python 3.10+, `ffmpeg`/`ffprobe` with libx264, and `exiftool` on `PATH`.

```sh
pip install .
snaparchive ingest ~/Downloads/mydata~*.zip --archive ~/Pictures/Snapchat
```

## Commands

| command | does |
| --- | --- |
| `ingest INPUT... --archive DIR` | add memories missing from the archive. `--dry-run` lists them without changing anything. |
| `inbox --inbox DIR --exports DIR --archive DIR` | ingest everything in a drop folder once it has stopped changing, then move it to `--exports` (for cron) |
| `status --archive DIR` | count, oldest and newest memory, per-year totals, months with no memories |
| `verify INPUT... --archive DIR` | compare an export's list of memories with the archive and print the ones that are missing. Exits 1 if any are. |

Useful options: `--workers N` (default 2), `--since YYYY-MM-DD`, `--crf`,
`--preset`, `--no-gps`, `--no-download`, `--file-mode 0640`, `--state DIR`
(default `<archive>/../.snaparchive`).

Exit codes: `0` ok, `2` some memories failed (see the report), `3` another run
holds the lock.

## Archive layout

```
2025-07-04_213015_Video_7C41D09E.mp4
└─date─┘ └time┘ └type┘ └─ID──┘
```

- **Time:** capture time in UTC, from Snapchat's metadata.
- **ID:** the first 8 characters of Snapchat's memory UUID.

The name alone says which memory a file is. If the manifest is lost, the
archive is still the source of truth. Photos carry `DateTimeOriginal` in UTC
with `OffsetTimeOriginal: +00:00`, so photo apps can convert to local time.
Videos keep Snapchat's own QuickTime timestamps, which are UTC by definition.

## Good to know

- Snapchat changes its export format without notice. The reader is lenient by
  design and reports every memory it could not place, rather than skipping it
  silently.
- Download links in link-style exports expire after a few days. If you get
  "link expired", request a fresh export.
- To check that an older archive is complete, run `verify` against an all-time
  export. It only reads the export's list of memories and never touches the
  media, so it is quick even for a large export.

## Tests

```sh
docker build -t snaparchive .
docker run --rm -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src/src --entrypoint python3 \
    snaparchive -m unittest discover -s tests -v
```

The tests build fake exports (photos, full-range and rotated videos, WebP
overlays, split exports, a fake Snapchat download server) and check the
results pixel by pixel.

## Acknowledgements

Other projects mapped out the export format first:
[ToTheMax/Snapchat-All-Memories-Downloader](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader),
[andrefecto/Snapchat-Memories-Downloader](https://github.com/andrefecto/Snapchat-Memories-Downloader),
[shoeless03/snapchat-memory-downloader](https://github.com/shoeless03/snapchat-memory-downloader)
and [Zk2u/snatch](https://github.com/Zk2u/snatch).

Not affiliated with Snap Inc.

## License

MIT
