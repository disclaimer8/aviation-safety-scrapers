#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaibzm-ingest
. .venv/bin/activate
DB=/home/a1/aaibzm-ingest/aaibzm.db
PDFS=/home/a1/aaibzm-ingest/pdfs
python -m aaibzm_ingest.cli discover --db "$DB"
python -m aaibzm_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaibzm_ingest.cli parse    --db "$DB"
python -m aaibzm_ingest.cli build    --db "$DB"
# Phase 2: sync aaibzm.db into prod (project narratives + occurrences + SSR purge)
bash "$HOME/aaibzm-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaibzm-sync failed (non-fatal)"
