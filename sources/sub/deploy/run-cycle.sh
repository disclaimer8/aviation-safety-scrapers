#!/usr/bin/env bash
set -euo pipefail
cd /opt/sub
. .venv/bin/activate
DB=/opt/sub/sub.db
python -m sub_ingest.cli discover --db "$DB"
python -m sub_ingest.cli fetch    --db "$DB" --pdf-dir /opt/sub/pdfs
python -m sub_ingest.cli build    --db "$DB"
