# Drop-folder setup for Nextcloud All-in-One

With this setup you upload Snapchat's export ZIPs into a Nextcloud folder and
nothing else. A cron job notices them and imports them. It merges overlays and
writes dates and GPS. Nextcloud's file index and the Memories timeline get
refreshed, and you get a Nextcloud notification. The raw export is filed away
next to the archive in case it is ever needed again.

```
<ARCHIVE_ROOT>/
├── memories/       the archive (point the Memories timeline here)
├── _inbox/         upload export ZIPs here
│   └── rejected/   ZIPs that could not be read end up here
├── _exports/       processed exports, one folder per import
└── .snaparchive/   manifest, reports, logs, cached export metadata
```

## Requirements

- Docker on the host, and a user in the `docker` group.
- `ARCHIVE_ROOT` reachable inside Nextcloud. With AIO, that means adding it to
  `NEXTCLOUD_MOUNT` and creating a "Local" external storage for it.
- The Nextcloud `notifications` app (enabled by default) if you want notifications.

## Install

```sh
git clone https://github.com/jkhalil4/snaparchive.git ~/snaparchive
docker build -t snaparchive:latest ~/snaparchive

mkdir -p ~/.config/snaparchive ~/.local/state/snaparchive
cp ~/snaparchive/deploy/nextcloud-aio/nextcloud-aio.env.example ~/.config/snaparchive/nextcloud-aio.env
$EDITOR ~/.config/snaparchive/nextcloud-aio.env

# create the folders as www-data so Nextcloud can write to them
. ~/.config/snaparchive/nextcloud-aio.env
docker run --rm --user 33:0 --entrypoint mkdir -v "$ARCHIVE_ROOT:/data" snaparchive:latest \
    -p /data/memories /data/_inbox /data/_exports /data/.snaparchive

# see what the archive looks like now
docker run --rm --user 33:0 -v "$ARCHIVE_ROOT:/data" snaparchive:latest status --archive /data/memories

# check every 15 minutes
( crontab -l 2>/dev/null; echo "*/15 * * * * $HOME/snaparchive/deploy/nextcloud-aio/snaparchive-inbox.sh >> $HOME/.local/state/snaparchive/inbox.log 2>&1" ) | crontab -
```

## Use

1. Request an export from Snapchat (see the main README) and download every ZIP.
2. Upload all the ZIPs into `_inbox` through Nextcloud (web, desktop or phone).
3. About 10–25 minutes after the last upload finishes, a notification says how
   many memories were added.

The inbox waits until nothing in it has changed for `MIN_AGE` seconds before
starting. That way a split export is imported as one batch. If a part arrives
later anyway, it still gets dates and GPS, because export metadata is cached
in `.snaparchive/`.

## Import by hand

```sh
docker run --rm --user 33:0 -v "$ARCHIVE_ROOT:/data" snaparchive:latest \
    ingest /data/_exports/2026-10-20_031500 --archive /data/memories --dry-run
```

Drop `--dry-run` to actually import. Then rescan Nextcloud:

```sh
docker exec -u www-data nextcloud-aio-nextcloud php occ files:scan --path="/$NC_USER/files/$NC_ARCHIVE_PATH"
docker exec -u www-data nextcloud-aio-nextcloud php occ memories:index --user="$NC_USER" --path="/$NC_ARCHIVE_PATH/memories"
```
