#!/usr/bin/env bash
set -euo pipefail
cd /opt/ciaado
. .venv/bin/activate
DB=/opt/ciaado/ciaado.db
PDFS=/opt/ciaado/pdfs
python -m ciaado_ingest.cli discover --db "$DB"
python -m ciaado_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ciaado_ingest.cli parse    --db "$DB"
python -m ciaado_ingest.cli build    --db "$DB"
