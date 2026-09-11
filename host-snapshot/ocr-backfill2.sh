#!/usr/bin/env bash
# OCR backfill driver v2 — runs ON the mini-PC (user a1) under nohup, SEQUENTIAL.
# Fixes v1's stdin-eating bug (array iteration + </dev/null on every inner cmd) and
# heals stray status='new' rows left by the v1/subagent collision (fetch reprocesses
# them). Re-OCRs scanned PDFs on the remote host (hetzner). Does NOT sync to prod.
set -u
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
LOG="$HOME/ocr-backfill2.log"
echo "=== driver2 start $(date -u +%FT%TZ) ===" >> "$LOG"

# src  method(fetch|parse)  xvfb(0|1)   — remaining sources, tail then big-3
SRCS=(
 "ovv fetch 0"
 "ueim fetch 0"
 "shk fetch 0"
 "knkt fetch 0"
 "sacaa fetch 0"
 "nsia fetch 0"
 "dgaccl fetch 0"
 "ciaape parse 0"
 "gpiaaf fetch 1"
 "pkbwl fetch 0"
 "taic fetch 0"
 "sust fetch 0"
 "cenipa parse 1"
)

run_src() {
  local src="$1" method="$2" xvfb="$3"
  local dir="$HOME/${src}-ingest" db="$HOME/${src}-ingest/${src}.db" pkg="${src}_ingest" tbl="${src}_reports"
  [ -d "$dir" ] && [ -f "$db" ] || { echo "${src}: NO DIR/DB [SKIP]" >> "$LOG"; return; }
  cd "$dir" || return
  . .venv/bin/activate 2>/dev/null
  local pre; pre=$(sqlite3 "$db" "SELECT COUNT(*) FROM ${tbl} WHERE source_tier='scanned';" </dev/null)
  echo "${src}: START method=${method} xvfb=${xvfb} scanned=${pre} $(date -u +%H:%M:%S)" >> "$LOG"
  local XV=""; [ "$xvfb" = "1" ] && XV="xvfb-run -a"
  if [ "$method" = "parse" ]; then
    sqlite3 "$db" "UPDATE ${tbl} SET status='fetched' WHERE source_tier='scanned';" </dev/null
    $XV python -m ${pkg}.cli fetch --db "$db" --pdf-dir "$dir/pdfs" </dev/null >>"$LOG" 2>&1
    python -m ${pkg}.cli parse --db "$db" </dev/null >>"$LOG" 2>&1
  else
    sqlite3 "$db" "UPDATE ${tbl} SET status='new' WHERE source_tier='scanned';" </dev/null
    $XV python -m ${pkg}.cli fetch --db "$db" --pdf-dir "$dir/pdfs" </dev/null >>"$LOG" 2>&1
  fi
  python -m ${pkg}.cli build --db "$db" </dev/null >>"$LOG" 2>&1
  local tiers post
  tiers=$(sqlite3 "$db" "SELECT group_concat(source_tier||'='||c,', ') FROM (SELECT source_tier, COUNT(*) c FROM ${tbl} GROUP BY source_tier);" </dev/null)
  post=$(sqlite3 "$db" "SELECT COUNT(*) FROM ${tbl} WHERE source_tier='scanned';" </dev/null)
  echo "${src}: DONE scanned ${pre}->${post} recovered=$(( ${pre:-0} - ${post:-0} )) [${tiers}]" >> "$LOG"
  deactivate 2>/dev/null || true
}

for row in "${SRCS[@]}"; do
  set -- $row
  run_src "$1" "$2" "$3"
done
echo "=== driver2 done $(date -u +%FT%TZ) ===" >> "$LOG"
