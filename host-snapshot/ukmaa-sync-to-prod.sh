#!/usr/bin/env bash
# Sync mini-PC UKMAA (gov.uk service inquiries) scraper output into prod app.db.
set -euo pipefail
LOCK=/tmp/ukmaa-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ukmaa-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${UKMAA_PROD_SSH:?set UKMAA_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ukmaa-ingest/ukmaa.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[ukmaa-sync] source db not found: $SRC_DB"; exit 1; }
echo "[ukmaa-sync] scp $SRC_DB -> prod"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ukmaa-src.db
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ukmaa --source-db /tmp/ukmaa-src.db && rm -f /tmp/ukmaa-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[ukmaa-sync] done"
