#!/usr/bin/env bash
set -euo pipefail
cd /opt/ciaape
. .venv/bin/activate
DB=/opt/ciaape/ciaape.db
PDFS=/opt/ciaape/pdfs
python -m ciaape_ingest.cli discover --db "$DB"
python -m ciaape_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ciaape_ingest.cli parse    --db "$DB"
python -m ciaape_ingest.cli build    --db "$DB"
