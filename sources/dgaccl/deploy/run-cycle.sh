#!/usr/bin/env bash
set -euo pipefail
cd /opt/dgaccl
. .venv/bin/activate
DB=/opt/dgaccl/dgaccl.db
python -m dgaccl_ingest.cli discover --db "$DB"
python -m dgaccl_ingest.cli fetch    --db "$DB" --pdf-dir /opt/dgaccl/pdfs
python -m dgaccl_ingest.cli build    --db "$DB"
