#!/usr/bin/env bash
set -euo pipefail
cd /opt/ahac
. .venv/bin/activate
DB=/opt/ahac/ahac.db
PDFS=/opt/ahac/pdfs
python -m ahac_ingest.cli discover --db "$DB"
python -m ahac_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m ahac_ingest.cli build    --db "$DB"
