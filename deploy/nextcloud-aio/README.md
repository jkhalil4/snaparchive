# Nextcloud All-in-One drop folder

You upload export ZIPs to a Nextcloud folder. A cron job imports them,
rescans Nextcloud, reindexes the Memories app, and sends you a notification.

```
<ARCHIVE_ROOT>/
├── memories/       the archive (set it as the Memories timeline folder)
├── _inbox/         upload ZIPs here; unreadable ones move to _inbox/rejected/
├── _exports/       processed exports, one folder per import
└── .snaparchive/   manifest, reports, logs
```

You need Docker, with your user in the `docker` group. `ARCHIVE_ROOT` must be
reachable from Nextcloud. With AIO, add it to `NEXTCLOUD_MOUNT` and create a
"Local" external storage for it.

## Install

```sh
git clone https://github.com/jkhalil4/snaparchive.git ~/snaparchive
docker build -t snaparchive:latest ~/snaparchive

mkdir -p ~/.config/snaparchive ~/.local/state/snaparchive
cp ~/snaparchive/deploy/nextcloud-aio/nextcloud-aio.env.example ~/.config/snaparchive/nextcloud-aio.env
$EDITOR ~/.config/snaparchive/nextcloud-aio.env
. ~/.config/snaparchive/nextcloud-aio.env

# create the folders as www-data
docker run --rm --user 33:0 --entrypoint mkdir -v "$ARCHIVE_ROOT:/data" snaparchive:latest \
    -p /data/memories /data/_inbox /data/_exports /data/.snaparchive

# run every 15 minutes
( crontab -l 2>/dev/null; echo "*/15 * * * * $HOME/snaparchive/deploy/nextcloud-aio/snaparchive-inbox.sh >> $HOME/.local/state/snaparchive/inbox.log 2>&1" ) | crontab -
```

## Use

Upload every ZIP of an export to `_inbox`. An import starts once the folder has
been unchanged for `MIN_AGE` seconds (default 600). That way a split export is
processed as one batch. Each export's metadata is cached, so a part that
arrives late still gets dates and GPS.

To import by hand, drop `--dry-run` after checking the output, then rescan:

```sh
docker run --rm --user 33:0 -v "$ARCHIVE_ROOT:/data" snaparchive:latest \
    ingest /data/_exports/<folder> --archive /data/memories --dry-run
docker exec -u www-data nextcloud-aio-nextcloud php occ files:scan --path="/$NC_USER/files/$NC_ARCHIVE_PATH"
docker exec -u www-data nextcloud-aio-nextcloud php occ memories:index --user="$NC_USER" --path="/$NC_ARCHIVE_PATH/memories"
```
