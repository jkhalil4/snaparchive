#!/usr/bin/env bash
# Cron job: import Snapchat exports from <ARCHIVE_ROOT>/_inbox, then rescan
# Nextcloud, reindex Memories and send a notification. Silent when idle.
#
#   */15 * * * * /path/to/snaparchive-inbox.sh >> ~/.local/state/snaparchive/inbox.log 2>&1
set -euo pipefail

config="${SNAPARCHIVE_CONFIG:-$HOME/.config/snaparchive/nextcloud-aio.env}"
# shellcheck disable=SC1090
. "$config"
: "${ARCHIVE_ROOT:?set ARCHIVE_ROOT in $config}"
: "${NC_USER:?set NC_USER in $config}"
: "${NC_ARCHIVE_PATH:?set NC_ARCHIVE_PATH in $config}"
IMAGE="${IMAGE:-snaparchive:latest}"
NC_CONTAINER="${NC_CONTAINER:-nextcloud-aio-nextcloud}"
RUN_AS="${RUN_AS:-33:0}"  # www-data, so Nextcloud owns what gets written
MIN_AGE="${MIN_AGE:-600}"
NOTIFY="${NOTIFY:-1}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

exec 9>"${XDG_RUNTIME_DIR:-/tmp}/snaparchive-inbox.$(id -u).lock"
flock -n 9 || exit 0

occ() { docker exec -u www-data "$NC_CONTAINER" php occ "$@"; }
field() {
    python3 -c 'import json, sys; v = json.loads(sys.argv[1]).get(sys.argv[2]); print("" if v is None else v)' "$summary" "$1"
}

rc=0
# shellcheck disable=SC2086
summary=$(docker run --rm --user "$RUN_AS" -v "$ARCHIVE_ROOT:/data" "$IMAGE" inbox \
    --inbox /data/_inbox --exports /data/_exports --archive /data/memories --state /data/.snaparchive \
    --min-age "$MIN_AGE" $EXTRA_ARGS | tail -n 1) || rc=$?
summary="${summary:-"{\"status\": \"error\"}"}"
status=$(field status)
case "$status" in
    idle | waiting | busy) exit 0 ;;
esac
echo "$(date -Is) exit=$rc $summary"

occ files:scan --path="/$NC_USER/files/$NC_ARCHIVE_PATH" --quiet || echo "files:scan failed"
if [ "$status" = ingested ] && [ "$(field added)" != 0 ]; then
    occ memories:index --user="$NC_USER" --path="/$NC_ARCHIVE_PATH/memories" --quiet || echo "memories:index failed"
fi

[ "$NOTIFY" = 1 ] || exit 0
case "$status" in
    ingested)
        added=$(field added) failed=$(field failed)
        short="Snapchat backup: $added new memories added"
        [ "$failed" != 0 ] && short="Snapchat backup: $added added, $failed failed"
        long="Overlays merged: $(field overlays_merged). Already in the archive: $(field already_archived)."
        long="$long Listed but not in the export files: $(field listed_without_media). Newest memory: $(field newest)."
        long="$long Raw export kept in $(field exports_folder). Report: $(field report)"
        ;;
    rejected)
        short="Snapchat backup: an export ZIP could not be read"
        long="Moved to _inbox/rejected. The download was probably cut short; download it again and re-upload. $(field rejected)"
        ;;
    *)
        short="Snapchat backup: the import run failed"
        long="Exit code $rc. See the cron log and $ARCHIVE_ROOT/.snaparchive/logs/snaparchive.log."
        ;;
esac
long="${long//\/data\//$NC_ARCHIVE_PATH/}"  # show paths as they appear in Nextcloud
occ notification:generate "$NC_USER" "$short" -l "$long" >/dev/null || echo "notification failed"
