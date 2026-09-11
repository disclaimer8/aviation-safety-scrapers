#!/usr/bin/env bash
set -euo pipefail
gen() {
  local src="$1" name="$2"
  cat > "$HOME/${src}-sync-to-prod.sh" <<EOF
#!/usr/bin/env bash
# Sync mini-PC ${name} scraper output into prod app.db (generic builder).
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
echo "[${src}-sync] scp \$SRC_DB -> prod"
scp -q "\$SRC_DB" "\$PROD_SSH":/tmp/${src}-src.db
ssh "\$PROD_SSH" "set -euo pipefail; export PATH=\$PROD_NODE_BIN:\\\$PATH; cd \$PROD_REPO; rm -f /var/lib/flightfinder/backups/app.db.before-${src}-sync-* && sqlite3 \$PROD_DB \\".backup '/var/lib/flightfinder/backups/app.db.before-${src}-sync-\$TS'\\" && node scripts/build-source-narratives.js --source ${src} --source-db /tmp/${src}-src.db && node scripts/build-occurrences.js && rm -f /tmp/${src}-src.db && echo applied"
ssh "\$PROD_SSH" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null || true; nginx -t && nginx -s reload" || true
echo "[${src}-sync] done"
EOF
  chmod +x "$HOME/${src}-sync-to-prod.sh"; echo "wrote $HOME/${src}-sync-to-prod.sh"
}
gen israel "Israel AIAI (gov.il)"
gen mexico "Mexico AFAC (gob.mx)"
