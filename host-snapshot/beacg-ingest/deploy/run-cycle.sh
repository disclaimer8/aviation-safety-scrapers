#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/beacg-ingest
. .venv/bin/activate
DB=/home/a1/beacg-ingest/beacg.db
PDFS=/home/a1/beacg-ingest/pdfs
python -m beacg_ingest.cli discover --db "$DB"
python -m beacg_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m beacg_ingest.cli parse    --db "$DB"
python -m beacg_ingest.cli build    --db "$DB"
# Phase 2: sync beacg.db into prod (project narratives + occurrences + SSR purge)
bash "$HOME/beacg-sync-to-prod.sh" "$DB" || echo "[run-cycle] beacg-sync failed (non-fatal)"
