#!/usr/bin/env bash
# Sync the mini-PC's ciaiauy (Uruguay aviation accident investigation) scraper output into prod app.db (project narratives + rebuild occurrences).
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/ciaiauy-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ciaiauy-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${CIAIAUY_PROD_SSH:?set CIAIAUY_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ciaiauy-ingest/ciaiauy.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ciaiauy-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ciaiauy-sync] scp $SRC_DB -> prod:/tmp/ciaiauy-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ciaiauy-src.db

echo "[ciaiauy-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# dgacgt-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ciaiauy --source-db /tmp/ciaiauy-src.db && rm -f /tmp/ciaiauy-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ciaiauy-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ciaiauy-sync] smoke (narratives-stats ciaiauy count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ciaiauy":[0-9]*' | head -1 || true
echo "[ciaiauy-sync] done"
