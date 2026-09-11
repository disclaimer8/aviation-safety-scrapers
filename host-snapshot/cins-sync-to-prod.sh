#!/usr/bin/env bash
# Sync the mini-PC's CINS (Center for Investigation of Transport Accidents of Serbia, cins.gov.rs) scraper output into the live prod app.db,
# then project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw cins.db and lets prod's build-cins-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors otkes-sync-to-prod.sh's lock/backup discipline.
# ⚠️ CINS = Center for Investigation of Transport Accidents of Serbia (cins.gov.rs, Serbia).
# Each source has its own offset and OWN lock; this script uses /tmp/cins-sync.lock.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/cins-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[cins-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${CINS_PROD_SSH:?set CINS_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/cins-ingest/cins.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[cins-sync] source db not found: $SRC_DB"; exit 1; }

echo "[cins-sync] scp $SRC_DB -> prod:/tmp/cins-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/cins-src.db

echo "[cins-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# otkes-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source cins --source-db /tmp/cins-src.db && rm -f /tmp/cins-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[cins-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[cins-sync] smoke (narratives-stats cins count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"cins":[0-9]*' | head -1 || true
echo "[cins-sync] done"
