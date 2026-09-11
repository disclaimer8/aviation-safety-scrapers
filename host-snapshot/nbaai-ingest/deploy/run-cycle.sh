#!/usr/bin/env bash
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
set -euo pipefail
cd /home/a1/nbaai-ingest
. .venv/bin/activate
DB=/home/a1/nbaai-ingest/nbaai.db
PDFS=/home/a1/nbaai-ingest/pdfs
python -m nbaai_ingest.cli discover --db "$DB"
python -m nbaai_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m nbaai_ingest.cli parse    --db "$DB"
python -m nbaai_ingest.cli build    --db "$DB"
# Phase 2: sync freshly-built nbaai.db into prod (project narratives + occurrences)
if [ -f "$HOME/nbaai-sync-to-prod.sh" ]; then
  bash "$HOME/nbaai-sync-to-prod.sh" "$DB" || echo "[run-cycle] nbaai-sync failed (non-fatal)"
fi
# Phase 2: sync nbaai.db into prod
bash "$HOME/nbaai-sync-to-prod.sh" "$DB" || echo "[run-cycle] nbaai-sync failed (non-fatal)"
