#!/usr/bin/env bash
set -euo pipefail
cd /opt/ciaiac
. .venv/bin/activate
DB=/opt/ciaiac/ciaiac.db
PDFS=/opt/ciaiac/pdfs
python -m ciaiac_ingest.cli discover --db "$DB"
python -m ciaiac_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ciaiac_ingest.cli parse    --db "$DB"
python -m ciaiac_ingest.cli build    --db "$DB"
