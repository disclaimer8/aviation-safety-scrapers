#!/usr/bin/env bash
# Sync the mini-PC's AAIASB (Greece, aaiasb.eu) scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache (generic builder).
set -euo pipefail
LOCK=/tmp/aaiasb-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[aaiasb-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${AAIASB_PROD_SSH:?set AAIASB_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/aaiasb-ingest/aaiasb.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[aaiasb-sync] source db not found: $SRC_DB"; exit 1; }
echo "[aaiasb-sync] scp $SRC_DB -> prod:/tmp/aaiasb-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/aaiasb-src.db
echo "[aaiasb-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source aaiasb --source-db /tmp/aaiasb-src.db && rm -f /tmp/aaiasb-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
echo "[aaiasb-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[aaiasb-sync] smoke (narratives-stats aaiasb count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"aaiasb":[0-9]*' | head -1 || true
echo "[aaiasb-sync] done"
