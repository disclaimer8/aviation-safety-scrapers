#!/usr/bin/env bash
# eaaid-sync-to-prod.sh — sync Egypt EAAID local db to prod FlightFinder.
# Mirrors dgcakw-sync-to-prod.sh discipline.
# DO NOT run until P1 is verified (no prod changes in P1 scope).
set -euo pipefail

LOCK=/tmp/eaaid-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[eaaid-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${EAAID_PROD_SSH:?set EAAID_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/eaaid-ingest/eaaid.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[eaaid-sync] source db not found: $SRC_DB"; exit 1; }

echo "[eaaid-sync] scp $SRC_DB -> prod:/tmp/eaaid-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/eaaid-src.db

echo "[eaaid-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source eaaid --source-db /tmp/eaaid-src.db && rm -f /tmp/eaaid-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[eaaid-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[eaaid-sync] smoke (narratives-stats eaaid count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"eaaid":[0-9]*' | head -1 || true
echo "[eaaid-sync] done"
