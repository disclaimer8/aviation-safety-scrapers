#!/usr/bin/env bash
set -euo pipefail
cd /opt/india
. .venv/bin/activate
DB=/opt/india/india.db
python -m india_ingest.cli discover --db "$DB"
python -m india_ingest.cli fetch    --db "$DB" --pdf-dir /opt/india/pdfs
python -m india_ingest.cli build    --db "$DB"
