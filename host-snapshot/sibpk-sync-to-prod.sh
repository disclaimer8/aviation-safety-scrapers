#!/usr/bin/env bash
# Sync the mini-PC sibpk (Pakistan SIB/BASI/AAIB) scraper output into prod app.db,
# then project narratives + rebuild occurrences + purge the SSR cache.
set -euo pipefail
LOCK=/tmp/sibpk-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[sibpk-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${SIBPK_PROD_SSH:?set SIBPK_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/sibpk-ingest/sibpk.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[sibpk-sync] source db not found: $SRC_DB"; exit 1; }
echo "[sibpk-sync] scp $SRC_DB -> prod:/tmp/sibpk-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/sibpk-src.db
echo "[sibpk-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source sibpk --source-db /tmp/sibpk-src.db && rm -f /tmp/sibpk-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
echo "[sibpk-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[sibpk-sync] smoke (narratives-stats sibpk count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"sibpk":[0-9]*' | head -1 || true
echo "[sibpk-sync] done"
