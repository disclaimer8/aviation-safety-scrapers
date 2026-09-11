#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaiubg-ingest
. .venv/bin/activate
DB=/home/a1/aaiubg-ingest/aaiubg.db
PDFS=/home/a1/aaiubg-ingest/pdfs
python -m aaiubg_ingest.cli discover --db "$DB"
python -m aaiubg_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaiubg_ingest.cli parse    --db "$DB"
python -m aaiubg_ingest.cli build    --db "$DB"
# Phase 2 (sync into prod) is added in a later phase; P1 = discover+fetch+parse+build only.
# Phase 2: sync aaiubg.db into prod
bash "$HOME/aaiubg-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaiubg-sync failed (non-fatal)"
