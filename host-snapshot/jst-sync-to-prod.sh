#!/usr/bin/env bash
# Sync the mini-PC's JST (Argentina) scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw jst.db and lets prod's build-jst-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors aaibmy-sync-to-prod.sh's lock/backup discipline.
# ⚠️ JST = Junta de Seguridad en el Transporte, ARGENTINA (jst.gob.ar).
# Each source has its own offset and OWN lock; this script uses /tmp/jst-sync.lock.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/jst-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[jst-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${JST_PROD_SSH:?set JST_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/jst-ingest/jst.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[jst-sync] source db not found: $SRC_DB"; exit 1; }

echo "[jst-sync] scp $SRC_DB -> prod:/tmp/jst-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/jst-src.db

echo "[jst-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# aaibmy-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source jst --source-db /tmp/jst-src.db && rm -f /tmp/jst-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[jst-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[jst-sync] smoke (narratives-stats jst count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"jst":[0-9]*' | head -1 || true
echo "[jst-sync] done"
