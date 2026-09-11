#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaiahk-ingest
. .venv/bin/activate
DB=/home/a1/aaiahk-ingest/aaiahk.db
PDFS=/home/a1/aaiahk-ingest/pdfs
python -m aaiahk_ingest.cli discover --db "$DB"
python -m aaiahk_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaiahk_ingest.cli parse    --db "$DB"
python -m aaiahk_ingest.cli build    --db "$DB"
# Phase 2 (separate task): sync freshly-built aaiahk.db into prod
if [ -f "$HOME/aaiahk-sync-to-prod.sh" ]; then
  bash "$HOME/aaiahk-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaiahk-sync failed (non-fatal)"
fi
