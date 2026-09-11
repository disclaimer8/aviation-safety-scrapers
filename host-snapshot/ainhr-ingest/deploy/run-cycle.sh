#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/ainhr-ingest
. .venv/bin/activate
DB=/home/a1/ainhr-ingest/ainhr.db
PDFS=/home/a1/ainhr-ingest/pdfs
python -m ainhr_ingest.cli discover --db "$DB"
python -m ainhr_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ainhr_ingest.cli parse    --db "$DB"
python -m ainhr_ingest.cli build    --db "$DB"
# Phase 2 (prod projection) is added later, mirroring ciaiac-sync-to-prod.sh.
# Phase 2: sync ainhr.db into prod
bash "$HOME/ainhr-sync-to-prod.sh" "$DB" || echo "[run-cycle] ainhr-sync failed (non-fatal)"
