#!/usr/bin/env bash
set -euo pipefail
gen() {
  local src="$1" name="$2"
  cat > "$HOME/${src}-sync-to-prod.sh" <<EOF
#!/usr/bin/env bash
# Sync the mini-PC's ${name} scraper output into the live prod app.db, then
# project narratives + rebuild occurrences + purge the SSR cache (generic builder).
set -euo pipefail
LOCK=/tmp/${src}-sync.lock
exec 9>"\$LOCK"
flock -n 9 || { echo "[${src}-sync] another sync in progress, skipping"; exit 0; }
PROD_SSH="\${${src^^}_PROD_SSH:-user@prod.example}"
SRC_DB="\${1:-\$HOME/${src}-ingest/${src}.db}"
PROD_DB=/var/lib/flightfinder/data/app.db
PROD_REPO=/root/flightfinder/server
PROD_NODE_BIN=/root/.nvm/versions/node/v24.14.0/bin
TS=\$(date -u +%Y%m%d-%H%M%S)
[ -f "\$SRC_DB" ] || { echo "[${src}-sync] source db not found: \$SRC_DB"; exit 1; }
echo "[${src}-sync] scp \$SRC_DB -> prod:/tmp/${src}-src.db"
scp -q "\$SRC_DB" "\$PROD_SSH":/tmp/${src}-src.db
echo "[${src}-sync] backup prod app.db + project + rebuild occurrences"
ssh "\$PROD_SSH" "set -euo pipefail; export PATH=\$PROD_NODE_BIN:\\\$PATH; cd \$PROD_REPO; rm -f /var/lib/flightfinder/backups/app.db.before-${src}-sync-* && sqlite3 \$PROD_DB \\".backup '/var/lib/flightfinder/backups/app.db.before-${src}-sync-\$TS'\\" && node scripts/build-source-narratives.js --source ${src} --source-db /tmp/${src}-src.db && node scripts/build-occurrences.js && rm -f /tmp/${src}-src.db && echo applied"
echo "[${src}-sync] purge nginx SSR cache"
ssh "\$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[${src}-sync] smoke (narratives-stats ${src} count)"
curl -s https://himaxym.com/api/safety/narratives-stats | grep -o '"${src}":[0-9]*' | head -1 || true
echo "[${src}-sync] done"
EOF
  chmod +x "$HOME/${src}-sync-to-prod.sh"
  echo "wrote $HOME/${src}-sync-to-prod.sh"
}
gen aaiasb "AAIASB (Greece, aaiasb.eu)"
gen taits  "SIA Lithuania / TAITS (sia.lrv.lt)"
gen aias   "AIAS (Romania, aias.gov.ro)"
