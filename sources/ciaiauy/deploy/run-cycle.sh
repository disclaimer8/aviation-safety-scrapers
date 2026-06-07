#!/usr/bin/env bash
set -euo pipefail
cd /opt/ciaiauy
. .venv/bin/activate
DB=/opt/ciaiauy/ciaiauy.db
PDFS=/opt/ciaiauy/pdfs
python -m ciaiauy_ingest.cli discover --db "$DB"
python -m ciaiauy_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ciaiauy_ingest.cli parse    --db "$DB"
python -m ciaiauy_ingest.cli build    --db "$DB"
