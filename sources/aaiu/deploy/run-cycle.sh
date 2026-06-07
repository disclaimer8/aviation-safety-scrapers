#!/usr/bin/env bash
set -euo pipefail
cd /opt/aaiu
. .venv/bin/activate
DB=/opt/aaiu/aaiu.db
python -m aaiu_ingest.cli discover --db "$DB"
python -m aaiu_ingest.cli fetch    --db "$DB" --pdf-dir /opt/aaiu/pdfs
python -m aaiu_ingest.cli build    --db "$DB"
