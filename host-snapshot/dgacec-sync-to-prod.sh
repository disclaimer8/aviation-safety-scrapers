#!/usr/bin/env bash
# Sync mini-PC Ecuador DGAC (aviacioncivil.gob.ec / archive.org) scraper output
# into prod app.db (generic builder).
#
# Usage: ~/dgacec-sync-to-prod.sh [path/to/dgacec.db]
#
# DO NOT RUN until the detached fetch+parse+build unit completes and you have
# verified dgacec_accidents count is reasonable (expected ~60-90 rows).
set -euo pipefail
LOCK=/tmp/dgacec-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[dgacec-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="${DGACEC_PROD_SSH:?set DGACEC_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/dgacec-ingest/dgacec.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)
[ -f "$SRC_DB" ] || { echo "[dgacec-sync] source db not found: $SRC_DB"; exit 1; }
echo "[dgacec-sync] scp $SRC_DB -> prod"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/dgacec-src.db
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source dgacec --source-db /tmp/dgacec-src.db && rm -f /tmp/dgacec-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[dgacec-sync] done"
