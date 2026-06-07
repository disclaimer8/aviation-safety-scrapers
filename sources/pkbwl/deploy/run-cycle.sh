#!/usr/bin/env bash
set -euo pipefail
cd /opt/pkbwl
. .venv/bin/activate
DB=/opt/pkbwl/pkbwl.db
python -m pkbwl_ingest.cli discover --db "$DB"
python -m pkbwl_ingest.cli fetch    --db "$DB" --pdf-dir /opt/pkbwl/pdfs
python -m pkbwl_ingest.cli build    --db "$DB"
