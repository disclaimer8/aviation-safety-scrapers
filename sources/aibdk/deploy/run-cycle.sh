#!/usr/bin/env bash
set -euo pipefail
cd /opt/aibdk
. .venv/bin/activate
DB=/opt/aibdk/aibdk.db
python -m aibdk_ingest.cli discover --db "$DB"
python -m aibdk_ingest.cli fetch    --db "$DB" --pdf-dir /opt/aibdk/pdfs
python -m aibdk_ingest.cli build    --db "$DB"
