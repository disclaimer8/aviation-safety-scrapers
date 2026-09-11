#!/usr/bin/env bash
set -euo pipefail
cd /home/a1/aaicth-ingest
. .venv/bin/activate
DB=/home/a1/aaicth-ingest/aaicth.db
PDFS=/home/a1/aaicth-ingest/pdfs
export OCR_REMOTE="a1@$(ssh -o StrictHostKeyChecking=no -o BatchMode=yes hetzner 'hostname -I' 2>/dev/null | awk '{print $1}' || echo 'user@prod.example')"
# OCR_REMOTE is for remote tesseract-tha OCR on hetzner (Thai scanned PDFs)
export OCR_REMOTE="${OCR_REMOTE:?set OCR_REMOTE to user@host — redacted from the public archive}"
python -m aaicth_ingest.cli discover --db "$DB"
python -m aaicth_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m aaicth_ingest.cli parse    --db "$DB"
python -m aaicth_ingest.cli build    --db "$DB"
