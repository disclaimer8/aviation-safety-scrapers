#!/usr/bin/env bash
set -euo pipefail
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
cd /home/a1/aaicmv-ingest
. .venv/bin/activate
DB=/home/a1/aaicmv-ingest/aaicmv.db
PDFS=/home/a1/aaicmv-ingest/pdfs
python -m aaicmv_ingest.cli discover --db "$DB"
python -m aaicmv_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaicmv_ingest.cli parse    --db "$DB"
python -m aaicmv_ingest.cli build    --db "$DB"
