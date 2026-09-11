#!/usr/bin/env bash
# Sync the mini-PC's BPEA (Bureau Permanent d'Enquêtes d'Accidents, DR Congo) scraper
# output into the live prod app.db, then project narratives + rebuild occurrences.
# Ships the raw bpea.db and lets prod's build-source-narratives.js read it via ATTACH.
set -euo pipefail

LOCK=/tmp/bpea-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[bpea-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${BPEA_PROD_SSH:?set BPEA_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/bpea-ingest/bpea.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[bpea-sync] source db not found: $SRC_DB"; exit 1; }

echo "[bpea-sync] scp $SRC_DB -> prod:/tmp/bpea-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/bpea-src.db

echo "[bpea-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source bpea --source-db /tmp/bpea-src.db && rm -f /tmp/bpea-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[bpea-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[bpea-sync] smoke (narratives-stats bpea count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"bpea":[0-9]*' | head -1 || true
echo "[bpea-sync] done"
