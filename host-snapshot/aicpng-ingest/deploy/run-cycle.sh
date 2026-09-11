#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aicpng-ingest
. .venv/bin/activate
DB=/home/a1/aicpng-ingest/aicpng.db
PDFS=/home/a1/aicpng-ingest/pdfs
python -m aicpng_ingest.cli discover --db "$DB"
python -m aicpng_ingest.cli refetch  --db "$DB"
python -m aicpng_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aicpng_ingest.cli parse    --db "$DB"
python -m aicpng_ingest.cli build    --db "$DB"
# Phase 2 (prod sync) is intentionally NOT wired here — this is the P1 scraper.
# Phase 2: sync freshly-built aicpng.db into prod (mirror cenipa recipe).
bash "$HOME/aicpng-sync-to-prod.sh" "$DB" || echo "[run-cycle] aicpng-sync failed (non-fatal)"
