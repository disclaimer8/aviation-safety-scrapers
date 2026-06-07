#!/usr/bin/env bash
set -euo pipefail
cd /opt/griaa
. .venv/bin/activate
DB=/opt/griaa/griaa.db
PDFS=/opt/griaa/pdfs
python -m griaa_ingest.cli discover --db "$DB"
python -m griaa_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m griaa_ingest.cli parse    --db "$DB"
python -m griaa_ingest.cli build    --db "$DB"
