#!/usr/bin/env bash
# Sync the mini-PC's IACM (Mozambique, www.iacm.gov.mz) scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache (generic builder).
set -euo pipefail
LOCK=/tmp/iacm-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[iacm-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${IACM_PROD_SSH:?set IACM_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/iacm-ingest/iacm.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[iacm-sync] source db not found: $SRC_DB"; exit 1; }
echo "[iacm-sync] scp $SRC_DB -> prod:/tmp/iacm-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/iacm-src.db
echo "[iacm-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source iacm --source-db /tmp/iacm-src.db && rm -f /tmp/iacm-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
echo "[iacm-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[iacm-sync] smoke (narratives-stats iacm count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"iacm":[0-9]*' | head -1 || true
echo "[iacm-sync] done"
