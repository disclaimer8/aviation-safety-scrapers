#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/bdca-ingest
. .venv/bin/activate
DB=/home/a1/bdca-ingest/bdca.db
PDFS=/home/a1/bdca-ingest/pdfs
python -m bdca_ingest.cli discover --db "$DB"
python -m bdca_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m bdca_ingest.cli parse    --db "$DB"
python -m bdca_ingest.cli build    --db "$DB"
# Phase 2: sync bdca.db into prod
bash "$HOME/bdca-sync-to-prod.sh" "$DB" || echo "[run-cycle] bdca-sync failed (non-fatal)"
