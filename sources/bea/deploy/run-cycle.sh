#!/usr/bin/env bash
set -euo pipefail
cd /opt/bea
. .venv/bin/activate
DB=/opt/bea/bea.db
PDFS=/opt/bea/pdfs
python -m bea_ingest.cli discover --db "$DB"
python -m bea_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m bea_ingest.cli parse    --db "$DB"
python -m bea_ingest.cli build    --db "$DB"
