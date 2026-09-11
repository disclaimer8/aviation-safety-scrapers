#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/eccaa-ingest
. .venv/bin/activate
DB=/home/a1/eccaa-ingest/eccaa.db
PDFS=/home/a1/eccaa-ingest/pdfs
python -m eccaa_ingest.cli discover --db "$DB"
python -m eccaa_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m eccaa_ingest.cli parse    --db "$DB"
python -m eccaa_ingest.cli build    --db "$DB"
# Phase 2 (sync to prod) is added separately; P1 cycle is discover->fetch->parse->build only.
# Phase 2: sync eccaa.db into prod
bash "$HOME/eccaa-sync-to-prod.sh" "$DB" || echo "[run-cycle] eccaa-sync failed (non-fatal)"
