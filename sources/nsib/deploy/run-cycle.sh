#!/usr/bin/env bash
set -euo pipefail
cd /opt/nsib
. .venv/bin/activate
DB=/opt/nsib/nsib.db
PDFS=/opt/nsib/pdfs
python -m nsib_ingest.cli discover --db "$DB"
python -m nsib_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m nsib_ingest.cli build    --db "$DB"
