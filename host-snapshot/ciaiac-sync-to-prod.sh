#!/usr/bin/env bash
# Sync the mini-PC's CIAIAC scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw ciaiac.db and lets prod's build-ciaiac-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors tsb-sync-to-prod.sh's lock/backup discipline.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/ciaiac-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ciaiac-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${CIAIAC_PROD_SSH:?set CIAIAC_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ciaiac-ingest/ciaiac.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ciaiac-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ciaiac-sync] scp $SRC_DB -> prod:/tmp/ciaiac-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ciaiac-src.db

echo "[ciaiac-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# tsb-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ciaiac --source-db /tmp/ciaiac-src.db && rm -f /tmp/ciaiac-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ciaiac-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ciaiac-sync] smoke (narratives-stats ciaiac count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ciaiac":[0-9]*' | head -1 || true
echo "[ciaiac-sync] done"
