#!/usr/bin/env bash
# Batch-project all OCR-recovered source DBs into prod, DISK-SAFE:
# ONE app.db backup, per-source scp+project+rm (no DB accumulation, no per-source
# occurrences rebuild), then ONE build-occurrences + ONE nginx purge at the end.
# Avoids the 22x3.5G backup disk-trap and 22 serial global occurrence rebuilds.
set -uo pipefail
PROD=user@prod.example
PROD_NODE=/root/.nvm/versions/node/v24.14.0/bin
PROD_DB=/var/lib/flightfinder/data/app.db
REPO=/root/flightfinder/server
TS=$(date -u +%Y%m%d-%H%M%S)
LOG="$HOME/batch-project.log"
SRCS="aaibmy aaisl gcaa dgacgt ovv sub otkes aaicmv aaiu india ueim shk knkt sacaa nsia dgaccl ciaape gpiaaf pkbwl taic cenipa sust"
: > "$LOG"
log(){ echo "$(date -u +%H:%M:%S) $*" | tee -a "$LOG"; }
log "=== batch-project start $TS ==="

# 1. ONE backup (rotate prior batch backup so the volume never accumulates)
log "[backup] app.db -> before-ocr-batch-$TS"
ssh "$PROD" "rm -f /var/lib/flightfinder/backups/app.db.before-ocr-batch-* && sqlite3 $PROD_DB \".backup '/var/lib/flightfinder/backups/app.db.before-ocr-batch-$TS'\"" >>"$LOG" 2>&1 \
  || { log "[backup] FAILED — abort"; exit 1; }

# 2. per source: scp + project (2 retries for transient SQLITE_BUSY) + rm
for s in $SRCS; do
  db="$HOME/$s-ingest/$s.db"
  [ -f "$db" ] || { log "[$s] no db, skip"; continue; }
  scp -q "$db" "$PROD:/tmp/$s-src.db" || { log "[$s] scp FAIL"; continue; }
  ok=0
  for try in 1 2 3; do
    if ssh "$PROD" "set -uo pipefail; export PATH=$PROD_NODE:\$PATH; cd $REPO; node scripts/build-source-narratives.js --source $s --source-db /tmp/$s-src.db" >>"$LOG" 2>&1; then
      ok=1; break
    fi
    log "[$s] project attempt $try failed (retrying)"; sleep 5
  done
  ssh "$PROD" "rm -f /tmp/$s-src.db" >>"$LOG" 2>&1 || true
  [ "$ok" = 1 ] && log "[$s] projected" || log "[$s] PROJECT FAIL after retries"
done

# 3. ONE global occurrences rebuild + nginx purge
log "[occurrences] rebuild (global)"
if ssh "$PROD" "set -uo pipefail; export PATH=$PROD_NODE:\$PATH; cd $REPO; node scripts/build-occurrences.js" >>"$LOG" 2>&1; then
  log "[occurrences] done"
else
  log "[occurrences] FAIL — retry once"; sleep 10
  ssh "$PROD" "set -uo pipefail; export PATH=$PROD_NODE:\$PATH; cd $REPO; node scripts/build-occurrences.js" >>"$LOG" 2>&1 && log "[occurrences] done(2)" || log "[occurrences] FAIL(2)"
fi
log "[nginx] purge ssr cache"
ssh "$PROD" "find /var/cache/nginx/ssr -type f -delete 2>/dev/null; nginx -t && nginx -s reload" >>"$LOG" 2>&1 || true
log "=== batch-project done $(date -u +%FT%TZ) ==="
