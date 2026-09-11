#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/taiib-ingest
. .venv/bin/activate
DB=/home/a1/taiib-ingest/taiib.db
PDFS=/home/a1/taiib-ingest/pdfs
python -m taiib_ingest.cli discover --db "$DB"
python -m taiib_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m taiib_ingest.cli parse    --db "$DB"
python -m taiib_ingest.cli build    --db "$DB"
# Phase 2: sync taiib.db into prod
bash "$HOME/taiib-sync-to-prod.sh" "$DB" || echo "[run-cycle] taiib-sync failed (non-fatal)"
