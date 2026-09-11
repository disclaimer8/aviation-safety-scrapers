#!/usr/bin/env bash
# dgcakw-sync-to-prod.sh — sync Kuwait DGCA local db to prod FlightFinder.
# Mirrors bdca-sync-to-prod.sh discipline.
set -euo pipefail

LOCK=/tmp/dgcakw-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[dgcakw-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${DGCAKW_PROD_SSH:?set DGCAKW_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/dgcakw-ingest/dgcakw.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[dgcakw-sync] source db not found: $SRC_DB"; exit 1; }

echo "[dgcakw-sync] scp $SRC_DB -> prod:/tmp/dgcakw-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/dgcakw-src.db

echo "[dgcakw-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source dgcakw --source-db /tmp/dgcakw-src.db && rm -f /tmp/dgcakw-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[dgcakw-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[dgcakw-sync] smoke (narratives-stats dgcakw count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"dgcakw":[0-9]*' | head -1 || true
echo "[dgcakw-sync] done"
