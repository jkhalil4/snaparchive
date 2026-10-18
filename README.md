# snaparchive

Updates a local archive of Snapchat Memories from Snapchat's data export. It
adds only the memories the archive doesn't have yet, merges overlays
(captions, stickers, drawings) back onto the media, and writes the capture
date and GPS into each file.

Snapchat caps free Memories storage at 5 GB. Starting January 2027, Memories
older than a year that fall outside your oldest 5 GB show only as thumbnails
until you pay or download them
([Snapchat Support](https://help.snapchat.com/hc/en-us/articles/41291271694228-How-do-I-manage-my-Memories-storage)).
The export delivers overlays as separate files and keeps dates and locations
only in JSON.

## Features

- Reads export ZIPs directly, including exports split across several ZIPs.
  Also reads extracted folders and `memories_history.json`/`.html`, from both
  exports with bundled media and older ones with download links.
- Skips memories already in the archive. Matches are by Snapchat's media ID,
  or by capture time for files named another way.
- Merges overlays onto photos (Pillow) and videos (ffmpeg, H.264 CRF 18).
  Memories without an overlay are copied unchanged.
- Writes the capture time (UTC) and GPS into each file and sets the file's
  modification time.
- Never overwrites a file. If an overlay can't be merged, the original is
  archived and the failure is reported.
- Writes a JSON report for each run and a manifest with the SHA-256 of every
  file added.
- Has a drop-folder mode for cron, plus a Nextcloud All-in-One wrapper that
  rescans Nextcloud, reindexes Memories, and sends a notification
  ([deploy/nextcloud-aio](deploy/nextcloud-aio/README.md)).

## Getting an export

1. Open **My Data** on [accounts.snapchat.com](https://accounts.snapchat.com),
   or in the app under Settings → My Data.
2. Turn on **Export your Memories**, and **Export JSON Files** if shown.
3. Click **Request Only Memories**. Pick a date range that starts a day or two
   before your newest archived memory.
4. Download every ZIP from the email. Large exports are split across several.

## Usage

The Docker image includes ffmpeg and exiftool:

```sh
docker build -t snaparchive .
docker run --rm -v /path/to/archive:/archive -v /path/to/downloads:/in:ro \
    snaparchive ingest /in --archive /archive/memories --dry-run
```

Remove `--dry-run` to import. `--user UID:GID` sets the owner of new files.

Without Docker, it needs Python 3.10+, ffmpeg with libx264, and exiftool:

```sh
pip install .
snaparchive ingest ~/Downloads/mydata~*.zip --archive ~/Pictures/Snapchat
```

| command | description |
| --- | --- |
| `ingest INPUT... --archive DIR` | add missing memories (`--dry-run` to preview) |
| `inbox --inbox DIR --exports DIR --archive DIR` | once the folder stops changing, ingest it and move its files to `--exports` |
| `status --archive DIR` | counts, oldest and newest memory, months with no memories |
| `verify INPUT... --archive DIR` | list memories in an export that the archive lacks (exit 1 if any) |

Options: `--workers N` (default 2), `--since YYYY-MM-DD`, `--crf`, `--preset`,
`--no-gps`, `--no-download`, `--file-mode 0640`, and `--state DIR` (default
`<archive>/../.snaparchive`).

Exit codes: 0 ok, 2 some memories failed, 3 another run is in progress.

## File names and metadata

Example: `2025-07-04_213015_Video_7C41D09E.mp4`

- `2025-07-04_213015`: capture date and time, in UTC
- `Video`: media type (`Image` or `Video`)
- `7C41D09E`: the first 8 characters of Snapchat's memory ID

Photos get `DateTimeOriginal` in UTC with `OffsetTimeOriginal: +00:00`.
Videos keep Snapchat's QuickTime timestamps, which are already UTC.

## Notes

- Download links in link-only exports expire after a few days. If you see
  "link expired", request a new export.
- To check an older archive for gaps, run `verify` against an all-time export.
  It reads only the memory list, not the media.

## Tests

```sh
docker run --rm -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src/src --entrypoint python3 \
    snaparchive -m unittest discover -s tests
```

## Acknowledgements

These projects documented the export format:
[ToTheMax/Snapchat-All-Memories-Downloader](https://github.com/ToTheMax/Snapchat-All-Memories-Downloader),
[andrefecto/Snapchat-Memories-Downloader](https://github.com/andrefecto/Snapchat-Memories-Downloader),
[shoeless03/snapchat-memory-downloader](https://github.com/shoeless03/snapchat-memory-downloader)
and [Zk2u/snatch](https://github.com/Zk2u/snatch).

Not affiliated with Snap Inc. MIT license.
