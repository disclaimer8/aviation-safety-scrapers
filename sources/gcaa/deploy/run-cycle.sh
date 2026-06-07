#!/usr/bin/env bash
set -euo pipefail
cd /opt/gcaa
. .venv/bin/activate
DB=/opt/gcaa/gcaa.db
python -m gcaa_ingest.cli discover --db "$DB"
python -m gcaa_ingest.cli fetch    --db "$DB" --pdf-dir /opt/gcaa/pdfs
python -m gcaa_ingest.cli build    --db "$DB"
