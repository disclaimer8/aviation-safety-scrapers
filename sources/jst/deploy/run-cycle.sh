#!/usr/bin/env bash
set -euo pipefail
cd /opt/jst
. .venv/bin/activate
DB=/opt/jst/jst.db
python -m jst_ingest.cli discover --db "$DB"
python -m jst_ingest.cli fetch    --db "$DB" --pdf-dir /opt/jst/pdfs
python -m jst_ingest.cli build    --db "$DB"
