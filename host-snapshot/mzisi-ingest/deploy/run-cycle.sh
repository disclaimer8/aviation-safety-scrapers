#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/mzisi-ingest
. .venv/bin/activate
DB=/home/a1/mzisi-ingest/mzisi.db
PDFS=/home/a1/mzisi-ingest/pdfs
python -m mzisi_ingest.cli discover --db "$DB"
python -m mzisi_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m mzisi_ingest.cli parse    --db "$DB"
python -m mzisi_ingest.cli build    --db "$DB"
# Phase 2: sync mzisi.db into prod
bash "$HOME/mzisi-sync-to-prod.sh" "$DB" || echo "[run-cycle] mzisi-sync failed (non-fatal)"
