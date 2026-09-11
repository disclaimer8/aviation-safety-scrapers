#!/usr/bin/env bash
# Sync the mini-PC's SIA Lithuania / TAITS (sia.lrv.lt) scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache (generic builder).
set -euo pipefail
LOCK=/tmp/taits-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[taits-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${TAITS_PROD_SSH:?set TAITS_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/taits-ingest/taits.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[taits-sync] source db not found: $SRC_DB"; exit 1; }
echo "[taits-sync] scp $SRC_DB -> prod:/tmp/taits-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/taits-src.db
echo "[taits-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source taits --source-db /tmp/taits-src.db && rm -f /tmp/taits-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
echo "[taits-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[taits-sync] smoke (narratives-stats taits count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"taits":[0-9]*' | head -1 || true
echo "[taits-sync] done"
