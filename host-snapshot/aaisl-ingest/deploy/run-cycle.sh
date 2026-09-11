#!/usr/bin/env bash
set -euo pipefail
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
cd /home/a1/aaisl-ingest
. .venv/bin/activate
DB=/home/a1/aaisl-ingest/aaisl.db
PDFS=/home/a1/aaisl-ingest/pdfs
python -m aaisl_ingest.cli discover --db "$DB"
python -m aaisl_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaisl_ingest.cli parse    --db "$DB"
python -m aaisl_ingest.cli build    --db "$DB"
# Phase 2 (prod sync) is a separate downstream step (mirror ciaiac-sync-to-prod.sh);
# intentionally NOT invoked here for the P1 scraper deliverable.
