#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/baaid-ingest
. .venv/bin/activate
DB=/home/a1/baaid-ingest/baaid.db
PDFS=/home/a1/baaid-ingest/pdfs
python -m baaid_ingest.cli discover --db "$DB"
python -m baaid_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m baaid_ingest.cli parse    --db "$DB"
python -m baaid_ingest.cli build    --db "$DB"
# Phase 2: sync baaid.db into prod
bash "$HOME/baaid-sync-to-prod.sh" "$DB" || echo "[run-cycle] baaid-sync failed (non-fatal)"
