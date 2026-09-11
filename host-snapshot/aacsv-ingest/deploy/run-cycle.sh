#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aacsv-ingest
. .venv/bin/activate
DB=/home/a1/aacsv-ingest/aacsv.db
PDFS=/home/a1/aacsv-ingest/pdfs
python -m aacsv_ingest.cli discover --db "$DB"
python -m aacsv_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aacsv_ingest.cli parse    --db "$DB"
python -m aacsv_ingest.cli build    --db "$DB"
# Phase 2: sync aacsv.db into prod
bash "$HOME/aacsv-sync-to-prod.sh" "$DB" || echo "[run-cycle] aacsv-sync failed (non-fatal)"
