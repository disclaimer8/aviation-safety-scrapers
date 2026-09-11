#!/usr/bin/env bash
# Sync the mini-PC's aaicth scraper output into prod app.db (project narratives + rebuild occurrences).
set -euo pipefail

LOCK=/tmp/aaicth-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[aaicth-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${AAICTH_PROD_SSH:?set AAICTH_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/aaicth-ingest/aaicth.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[aaicth-sync] source db not found: $SRC_DB"; exit 1; }

echo "[aaicth-sync] scp $SRC_DB -> prod:/tmp/aaicth-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/aaicth-src.db

echo "[aaicth-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source aaicth --source-db /tmp/aaicth-src.db && rm -f /tmp/aaicth-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[aaicth-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[aaicth-sync] smoke (narratives-stats aaicth count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"aaicth":[0-9]*' | head -1 || true
echo "[aaicth-sync] done"
