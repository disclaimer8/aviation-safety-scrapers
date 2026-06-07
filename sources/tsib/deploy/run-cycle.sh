#!/usr/bin/env bash
set -euo pipefail
cd /opt/tsib
. .venv/bin/activate
DB=/opt/tsib/tsib.db
python -m tsib_ingest.cli discover --db "$DB"
python -m tsib_ingest.cli fetch    --db "$DB" --pdf-dir /opt/tsib/pdfs
python -m tsib_ingest.cli build    --db "$DB"
