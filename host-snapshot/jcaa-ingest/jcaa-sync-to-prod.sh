#!/usr/bin/env bash
# Sync the mini-PC's jcaa scraper output into prod app.db (project narratives + rebuild occurrences).
set -euo pipefail

LOCK=/tmp/jcaa-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[jcaa-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${JCAA_PROD_SSH:?set JCAA_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/jcaa-ingest/jcaa.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[jcaa-sync] source db not found: $SRC_DB"; exit 1; }

echo "[jcaa-sync] scp $SRC_DB -> prod:/tmp/jcaa-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/jcaa-src.db

echo "[jcaa-sync] project jcaa narratives (occurrences rebuilt by the batch cron)"
# Batch model: project this source's narratives and flag the occurrences table
# dirty. The single prod cron (build-occurrences-if-dirty) does ONE backup + ONE
# build-occurrences for the whole burst, instead of every source rebuilding all
# ~142K occurrences and snapshotting the 3.7G app.db.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source jcaa --source-db /tmp/jcaa-src.db && rm -f /tmp/jcaa-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[jcaa-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[jcaa-sync] smoke (narratives-stats jcaa count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"jcaa":[0-9]*' | head -1 || true
echo "[jcaa-sync] done"
