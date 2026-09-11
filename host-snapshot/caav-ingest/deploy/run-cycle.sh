#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/caav-ingest
. .venv/bin/activate
DB=/home/a1/caav-ingest/caav.db
PDFS=/home/a1/caav-ingest/pdfs
python -m caav_ingest.cli discover --db "$DB"
python -m caav_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m caav_ingest.cli parse    --db "$DB"
python -m caav_ingest.cli build    --db "$DB"
# Phase 2 (prod projection sync) is added when the prod side is built, mirroring
# ciaiac-sync-to-prod.sh.  Not part of P1.
# Phase 2: sync freshly-built caav.db into prod (mirror cenipa recipe).
bash "$HOME/caav-sync-to-prod.sh" "$DB" || echo "[run-cycle] caav-sync failed (non-fatal)"
