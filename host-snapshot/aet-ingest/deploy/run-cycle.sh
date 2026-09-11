#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aet-ingest
. .venv/bin/activate
DB=/home/a1/aet-ingest/aet.db
PDFS=/home/a1/aet-ingest/pdfs
python -m aet_ingest.cli discover --db "$DB"
python -m aet_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aet_ingest.cli parse    --db "$DB"
python -m aet_ingest.cli build    --db "$DB"
# Phase 2: sync aet.db into prod (project narratives + occurrences + SSR purge)
bash "$HOME/aet-sync-to-prod.sh" "$DB" || echo "[run-cycle] aet-sync failed (non-fatal)"
