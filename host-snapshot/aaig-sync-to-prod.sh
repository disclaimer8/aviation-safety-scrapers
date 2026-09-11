#!/usr/bin/env bash
# Sync mini-PC Bangladesh AAIG (Aircraft Accident Investigation Group, via Wayback Machine)
# scraper output into prod app.db (generic builder).
set -euo pipefail
LOCK=/tmp/aaig-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[aaig-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${AAIG_PROD_SSH:?set AAIG_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/aaig-ingest/aaig.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[aaig-sync] source db not found: $SRC_DB"; exit 1; }
echo "[aaig-sync] scp $SRC_DB -> prod"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/aaig-src.db
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source aaig --source-db /tmp/aaig-src.db && rm -f /tmp/aaig-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[aaig-sync] smoke (narratives-stats aaig count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"aaig":[0-9]*' | head -1 || true
echo "[aaig-sync] done"
