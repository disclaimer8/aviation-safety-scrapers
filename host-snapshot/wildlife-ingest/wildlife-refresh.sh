#!/usr/bin/env bash
# wildlife-refresh.sh — regenerate the FAA Wildlife Strike aggregate seed on the
# mini-PC. Downloads the public-domain database, rebuilds wildlife.db, re-folds it
# into server/data/wildlife-strikes.json. Run weekly (FAA refreshes continuously).
#
# The seed JSON is COMMITTED to the repo (like sdr-reliability.json) and loads on
# boot. Durable update = commit the regenerated seed. For between-deploy freshness,
# pass --push to scp it into the prod working tree and reload.
#
# Usage (on mini-PC):  bash wildlife-refresh.sh [--push]
set -euo pipefail
DIR="${WILDLIFE_DIR:-$HOME/wildlife-ingest}"
URL="https://wildlife.faa.gov/assets/database.zip"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
PROD_SSH="${WILDLIFE_PROD_SSH:?set WILDLIFE_PROD_SSH to user@host — redacted from the public archive}"
PROD_REPO="${WILDLIFE_PROD_REPO:-/root/flightfinder/server}"
PROD_NODE_BIN="${WILDLIFE_PROD_NODE_BIN:-/root/.nvm/versions/node/v24.14.0/bin}"
LOCK=/tmp/wildlife-refresh.lock
exec 9>"$LOCK"; flock -n 9 || { echo "[wildlife] another refresh in progress"; exit 0; }

mkdir -p "$DIR"; cd "$DIR"
echo "[wildlife] download $URL"
curl -fSs -A "$UA" -o database.zip "$URL"
file database.zip | grep -qi 'zip archive' || { echo "[wildlife] not a zip (FAA layout changed?)"; exit 1; }
rm -rf db && mkdir db && unzip -oq database.zip -d db
ACCDB=$(ls db/*.accdb | head -1)
echo "[wildlife] export $ACCDB"
mdb-export -D "%Y-%m-%d" "$ACCDB" STRIKE_REPORTS > strike_reports.csv
echo "[wildlife] load -> wildlife.db"
node load-wildlife.js strike_reports.csv wildlife.db
echo "[wildlife] aggregate -> wildlife-strikes.json"
node wildlife-aggregate.js wildlife.db wildlife-strikes.json

if [ "${1:-}" = "--push" ]; then
  echo "[wildlife] push seed -> prod (ephemeral until next deploy; commit for durability)"
  scp -q wildlife-strikes.json "$PROD_SSH":"$PROD_REPO/data/wildlife-strikes.json"
  ssh "$PROD_SSH" "export PATH=$PROD_NODE_BIN:\$PATH; cd $PROD_REPO; node -e \"require('./src/services/wildlifeStrikesService').loadSeedIntoDb()\"; find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -s reload 2>/dev/null || true"
fi
echo "[wildlife] done. Commit server/data/wildlife-strikes.json for a durable update."
