#!/usr/bin/env bash
# Sync the mini-PC's ttcaa scraper output into prod app.db (project narratives + rebuild occurrences).
set -euo pipefail

LOCK=/tmp/ttcaa-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ttcaa-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${TTCAA_PROD_SSH:?set TTCAA_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ttcaa-ingest/ttcaa.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ttcaa-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ttcaa-sync] scp $SRC_DB -> prod:/tmp/ttcaa-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ttcaa-src.db

echo "[ttcaa-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ttcaa --source-db /tmp/ttcaa-src.db && rm -f /tmp/ttcaa-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ttcaa-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ttcaa-sync] smoke (narratives-stats ttcaa count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ttcaa":[0-9]*' | head -1 || true
echo "[ttcaa-sync] done"
