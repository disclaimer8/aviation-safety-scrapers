#!/usr/bin/env bash
# Sync the mini-PC's ipiaam scraper output into prod app.db (project narratives + rebuild occurrences).
set -euo pipefail

LOCK=/tmp/ipiaam-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ipiaam-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${IPIAAM_PROD_SSH:?set IPIAAM_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ipiaam-ingest/ipiaam.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ipiaam-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ipiaam-sync] scp $SRC_DB -> prod:/tmp/ipiaam-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ipiaam-src.db

echo "[ipiaam-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ipiaam --source-db /tmp/ipiaam-src.db && rm -f /tmp/ipiaam-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ipiaam-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ipiaam-sync] smoke (narratives-stats ipiaam count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ipiaam":[0-9]*' | head -1 || true
echo "[ipiaam-sync] done"
