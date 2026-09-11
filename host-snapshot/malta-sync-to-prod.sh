#!/usr/bin/env bash
# Sync mini-PC Malta BAAI scraper output into prod app.db (generic builder).
set -euo pipefail
LOCK=/tmp/malta-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[malta-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${MALTA_PROD_SSH:?set MALTA_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/malta-ingest/malta.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[malta-sync] source db not found: $SRC_DB"; exit 1; }
echo "[malta-sync] scp $SRC_DB -> prod"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/malta-src.db
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source malta --source-db /tmp/malta-src.db && rm -f /tmp/malta-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[malta-sync] done"
