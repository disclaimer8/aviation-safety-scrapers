#!/usr/bin/env bash
# Sync the mini-PC's ANSV scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw ansv.db and lets prod's build-ansv-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors ciaiac-sync-to-prod.sh's lock/backup discipline.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/ansv-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ansv-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${ANSV_PROD_SSH:?set ANSV_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ansv-ingest/ansv.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ansv-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ansv-sync] scp $SRC_DB -> prod:/tmp/ansv-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ansv-src.db

echo "[ansv-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# ciaiac-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ansv --source-db /tmp/ansv-src.db && rm -f /tmp/ansv-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ansv-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ansv-sync] smoke (narratives-stats ansv count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ansv":[0-9]*' | head -1 || true
echo "[ansv-sync] done"
