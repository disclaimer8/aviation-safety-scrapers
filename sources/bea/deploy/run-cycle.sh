#!/usr/bin/env bash
set -euo pipefail
cd /opt/bea
. .venv/bin/activate
DB=/opt/bea/bea.db
PDFS=/opt/bea/pdfs
python -m bea_ingest.cli discover --db "$DB"
# Re-queue PDF-less stubs whose event page may have gained the report PDF
# since first sight (BEA attaches reports to existing pages months later).
python -m bea_ingest.cli refetch  --db "$DB"
python -m bea_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m bea_ingest.cli parse    --db "$DB"
python -m bea_ingest.cli build    --db "$DB"
