#!/usr/bin/env bash
# OCR backfill driver — runs ON the mini-PC (user a1) under nohup.
# Re-OCRs already-downloaded scanned PDFs on the remote OCR host (hetzner) and
# rebuilds each source's local DB. Does NOT sync to prod (parent batch-projects).
# Resumable: reset only touches still-'scanned' rows, so re-running skips recovered ones.
set -u
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
LOG="$HOME/ocr-backfill.log"
echo "=== driver start $(date -u +%FT%TZ) ===" >> "$LOG"

# src  method  xvfb   (smallest scanned first; big-3 last)
ROWS="
ovv     fetch  0
sub     fetch  0
otkes   fetch  1
aaicmv  parse  0
aaiu    fetch  0
india   fetch  0
shk     fetch  0
ueim    fetch  0
knkt    fetch  0
sacaa   fetch  0
nsia    fetch  0
dgaccl  fetch  0
ciaape  parse  0
gpiaaf  fetch  1
pkbwl   fetch  0
taic    fetch  0
cenipa  parse  0
sust    fetch  0
"

run_src() {
  local src="$1" method="$2" xvfb="$3"
  local dir="$HOME/${src}-ingest" db="$HOME/${src}-ingest/${src}.db" pkg="${src}_ingest" tbl="${src}_reports"
  [ -d "$dir" ] || { echo "${src}: NO DIR [SKIP]" >> "$LOG"; return; }
  [ -f "$db" ]  || { echo "${src}: NO DB [SKIP]"  >> "$LOG"; return; }
  cd "$dir" || return
  . .venv/bin/activate 2>/dev/null
  local pre; pre=$(sqlite3 "$db" "SELECT COUNT(*) FROM ${tbl} WHERE source_tier='scanned';" 2>/dev/null)
  echo "${src}: START method=${method} xvfb=${xvfb} scanned=${pre} $(date -u +%H:%M:%S)" >> "$LOG"
  local XV=""; [ "$xvfb" = "1" ] && XV="xvfb-run -a"
  if [ "$method" = "parse" ]; then
    sqlite3 "$db" "UPDATE ${tbl} SET status='fetched' WHERE source_tier='scanned';" 2>>"$LOG"
    $XV python -m ${pkg}.cli parse --db "$db" >>"$LOG" 2>&1
  else
    sqlite3 "$db" "UPDATE ${tbl} SET status='new' WHERE source_tier='scanned';" 2>>"$LOG"
    $XV python -m ${pkg}.cli fetch --db "$db" --pdf-dir "$dir/pdfs" >>"$LOG" 2>&1
  fi
  python -m ${pkg}.cli build --db "$db" >>"$LOG" 2>&1
  local tiers; tiers=$(sqlite3 "$db" "SELECT group_concat(source_tier||'='||c,', ') FROM (SELECT source_tier, COUNT(*) c FROM ${tbl} GROUP BY source_tier);" 2>/dev/null)
  local post; post=$(sqlite3 "$db" "SELECT COUNT(*) FROM ${tbl} WHERE source_tier='scanned';" 2>/dev/null)
  echo "${src}: DONE scanned ${pre}->${post} recovered=$(( ${pre:-0} - ${post:-0} )) [${tiers}]" >> "$LOG"
  deactivate 2>/dev/null || true
}

echo "$ROWS" | while read -r src method xvfb; do
  [ -z "$src" ] && continue
  run_src "$src" "$method" "$xvfb"
done
echo "=== driver done $(date -u +%FT%TZ) ===" >> "$LOG"
