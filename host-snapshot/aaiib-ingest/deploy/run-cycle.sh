#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaiib-ingest
. .venv/bin/activate
DB=/home/a1/aaiib-ingest/aaiib.db
PDFS=/home/a1/aaiib-ingest/pdfs
python -m aaiib_ingest.cli discover --db "$DB"
python -m aaiib_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaiib_ingest.cli parse    --db "$DB"
python -m aaiib_ingest.cli build    --db "$DB"
# Phase 2 (added later): sync freshly-built aaiib.db into prod (narratives + occurrences)
bash "$HOME/aaiib-sync-to-prod.sh" "$DB" || echo "[run-cycle] aaiib-sync failed (non-fatal)"
