#!/usr/bin/env bash
set -euo pipefail
cd /opt/uzpln
. .venv/bin/activate
DB=/opt/uzpln/uzpln.db
python -m uzpln_ingest.cli discover --db "$DB"
python -m uzpln_ingest.cli fetch    --db "$DB" --pdf-dir /opt/uzpln/pdfs
python -m uzpln_ingest.cli build    --db "$DB"
