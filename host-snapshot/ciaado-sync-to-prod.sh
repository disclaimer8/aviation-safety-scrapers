#!/usr/bin/env bash
# Sync the mini-PC's CIAADO (Gabinete de Prevencao e Investigacao de Acidentes com Aeronaves e de Acidentes Ferroviarios / Portuguese Office for the Prevention and Investigation of Accidents in Civil Aviation and Rail, ciaado.gov.pt) scraper output into the live prod app.db,
# then project narratives + rebuild occurrences + purge the SSR cache.
# Ships the raw ciaado.db and lets prod's build-ciaado-narratives.js read it via
# ATTACH (no portable-dump escaping; the integer id lives only on prod).
# Mirrors ciaiauy-sync-to-prod.sh's lock/backup discipline.
# ⚠️ CIAADO = Portuguese Office for the Prevention and Investigation of Accidents in Civil Aviation and Rail (ciaado.gov.pt, Dominican Republic).
# Each source has its own offset and OWN lock; this script uses /tmp/ciaado-sync.lock.
set -euo pipefail

# Self-lock: skip if another sync is already running. -n = non-blocking: a
# concurrent call exits 0 cleanly so `set -e` in any caller doesn't abort.
LOCK=/tmp/ciaado-sync.lock
exec 9>"$LOCK"
flock -n 9 || { echo "[ciaado-sync] another sync in progress, skipping"; exit 0; }

PROD_SSH="${CIAADO_PROD_SSH:?set CIAADO_PROD_SSH to user@host — redacted from the public archive}"
SRC_DB="${1:-$HOME/ciaado-ingest/ciaado.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
# Node on prod lives under nvm and is NOT in the non-interactive ssh PATH.
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=$(date -u +%Y%m%d-%H%M%S)

[ -f "$SRC_DB" ] || { echo "[ciaado-sync] source db not found: $SRC_DB"; exit 1; }

echo "[ciaado-sync] scp $SRC_DB -> prod:/tmp/ciaado-src.db"
scp -q "$SRC_DB" "$PROD_SSH":/tmp/ciaado-src.db

echo "[ciaado-sync] backup prod app.db + project + rebuild occurrences"
# ⚠️ Rotate before each backup (one snapshot per sync is enough; mirrors
# ciaiauy-sync-to-prod.sh's rotate discipline to avoid filling the volume).
# Remote command: explicit `set -euo pipefail` so fail-fast is enforced by the
# shell (not just the `&&` chain) — a future edit inserting a `;` step can't
# silently skip the abort. `cd $PROD_REPO` so the node scripts resolve their
# relative paths from the checkout root.
ssh "$PROD_SSH" "set -euo pipefail; export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node scripts/build-source-narratives.js --source ciaado --source-db /tmp/ciaado-src.db && rm -f /tmp/ciaado-src.db && touch /var/lib/flightfinder/data/.occurrences-dirty && echo applied"

echo "[ciaado-sync] purge nginx SSR cache"
ssh "$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true

echo "[ciaado-sync] smoke (narratives-stats ciaado count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"ciaado":[0-9]*' | head -1 || true
echo "[ciaado-sync] done"
