#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaicnp-ingest
. .venv/bin/activate
DB=/home/a1/aaicnp-ingest/aaicnp.db
PDFS=/home/a1/aaicnp-ingest/pdfs
python -m aaicnp_ingest.cli discover --db "$DB"
python -m aaicnp_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaicnp_ingest.cli parse    --db "$DB"
python -m aaicnp_ingest.cli build    --db "$DB"
# Phase 2: sync aaicnp.db into prod
bash "$HOME/aaicnp-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaicnp-sync failed (non-fatal)"
