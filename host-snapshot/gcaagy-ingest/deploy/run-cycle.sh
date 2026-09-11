#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/gcaagy-ingest
. .venv/bin/activate
DB=/home/a1/gcaagy-ingest/gcaagy.db
PDFS=/home/a1/gcaagy-ingest/pdfs
python -m gcaagy_ingest.cli discover --db "$DB"
python -m gcaagy_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m gcaagy_ingest.cli parse    --db "$DB"
python -m gcaagy_ingest.cli build    --db "$DB"
# Phase 2: sync gcaagy.db into prod
bash "$HOME/gcaagy-sync-to-prod.sh" "$DB" || echo "[run-cycle] gcaagy-sync failed (non-fatal)"
