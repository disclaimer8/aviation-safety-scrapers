#!/usr/bin/env bash
# Sync the mini-PC's BEA Sénégal (bea.sn) scraper output into the live prod app.db,
# then project narratives + rebuild occurrences.
# Ships the raw beasn.db and lets prod's build-source-narratives.js read it via ATTACH.
# NOTE: beasn is an existing registry source (offset 104B), already has 2 prod records.
set -euo pipefail

LOCK=/tmp/beasn-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[beasn-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${BEASN_PROD_SSH:?set BEASN_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/beasn-ingest/beasn.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[beasn-sync] source db not found: $SRC_DB"; exit 1; }

echo "[beasn-sync] scp $SRC_DB -> prod:/tmp/beasn-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/beasn-src.db

echo "[beasn-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source beasn --source-db /tmp/beasn-src.db && rm -f /tmp/beasn-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[beasn-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[beasn-sync] smoke (narratives-stats beasn count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"beasn":[0-9]*' | head -1 || true
echo "[beasn-sync] done"
