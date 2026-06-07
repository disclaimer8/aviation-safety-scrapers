#!/usr/bin/env bash
set -euo pipefail
cd /opt/jtsb
. .venv/bin/activate
DB=/opt/jtsb/jtsb.db
PDFS=/opt/jtsb/pdfs
python -m jtsb_ingest.cli discover --db "$DB"
python -m jtsb_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m jtsb_ingest.cli parse    --db "$DB"
python -m jtsb_ingest.cli build    --db "$DB"
