#!/usr/bin/env bash
set -euo pipefail
cd /opt/ansv
. .venv/bin/activate
DB=/opt/ansv/ansv.db
PDFS=/opt/ansv/pdfs
python -m ansv_ingest.cli discover --db "$DB"
python -m ansv_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ansv_ingest.cli parse    --db "$DB"
python -m ansv_ingest.cli build    --db "$DB"
