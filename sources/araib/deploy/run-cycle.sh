#!/usr/bin/env bash
set -euo pipefail
cd /opt/araib
. .venv/bin/activate
DB=/opt/araib/araib.db
python -m araib_ingest.cli discover --db "$DB"
python -m araib_ingest.cli fetch    --db "$DB" --pdf-dir /opt/araib/pdfs
python -m araib_ingest.cli build    --db "$DB"
