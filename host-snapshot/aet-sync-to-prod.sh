#!/usr/bin/env bash
# Sync the mini-PC's AET (Administration des enquetes techniques, Luxembourg, aet.gouvernement.lu) scraper output into the live prod app.db,
# then project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw aet.db and lets prod's build-aet-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors nsib-sync-to-prod.sh's lock/backup discipline.
# ⚠️ AET = Administration des enquetes techniques, Luxembourg (aet.gouvernement.lu, Luxembourg).
# build-aet-narratives.js applies a foreign-authority DROP-LIST at projection time
# (already-covered BEA FR / BFU DE / ANSV IT / CIAIAC ES reports) — the facts table
# stays complete; only the projection is filtered.
# Each source has its own offset and OWN lock; this script uses /tmp/aet-sync.lock.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/aet-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[aet-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${AET_PROD_SSH:?set AET_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/aet-ingest/aet.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[aet-sync] source db not found: $SRC_DB"; exit 1; }

echo "[aet-sync] scp $SRC_DB -> prod:/tmp/aet-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/aet-src.db

echo "[aet-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# nsib-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source aet --source-db /tmp/aet-src.db && rm -f /tmp/aet-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[aet-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[aet-sync] smoke (narratives-stats aet count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"aet":[0-9]*' | head -1 || true
echo "[aet-sync] done"
