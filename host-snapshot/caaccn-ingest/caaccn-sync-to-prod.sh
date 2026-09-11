#!/usr/bin/env bash
# Sync the mini-PC's CAAC (Civil Aviation Administration of China) + MEM scraper
# output into the live prod app.db, then project narratives + rebuild occurrences + purge SSR.
# Ships the raw caaccn.db and lets prod's build-source-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors bdca-sync-to-prod.sh lock/backup discipline.
# Each source has its own offset and OWN lock; this script uses /tmp/caaccn-sync.lock.
set -euo pipefail

LOCK=/tmp/caaccn-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[caaccn-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${CAAC_PROD_SSH:?set CAAC_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/caaccn-ingest/caaccn.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[caaccn-sync] source db not found: $SRC_DB"; exit 1; }

echo "[caaccn-sync] scp $SRC_DB -> prod:/tmp/caaccn-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/caaccn-src.db

echo "[caaccn-sync] backup prod app.db + project + rebuild occurrences"
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source caaccn --source-db /tmp/caaccn-src.db && rm -f /tmp/caaccn-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[caaccn-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[caaccn-sync] smoke (narratives-stats caaccn count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"caaccn":[0-9]*' | head -1 || true
echo "[caaccn-sync] done"
