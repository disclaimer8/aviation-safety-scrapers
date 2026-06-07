#!/usr/bin/env bash
set -euo pipefail
cd /opt/ueim
. .venv/bin/activate
DB=/opt/ueim/ueim.db
python -m ueim_ingest.cli discover --db "$DB"
python -m ueim_ingest.cli fetch    --db "$DB" --pdf-dir /opt/ueim/pdfs
python -m ueim_ingest.cli build    --db "$DB"
