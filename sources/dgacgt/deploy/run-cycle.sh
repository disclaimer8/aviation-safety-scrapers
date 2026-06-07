#!/usr/bin/env bash
set -euo pipefail
cd /opt/dgacgt
. .venv/bin/activate
DB=/opt/dgacgt/dgacgt.db
PDFS=/opt/dgacgt/pdfs
python -m dgacgt_ingest.cli discover --db "$DB"
python -m dgacgt_ingest.cli fetch    --db "$DB" --pdf-dir "$PDFS"
python -m dgacgt_ingest.cli parse    --db "$DB"
python -m dgacgt_ingest.cli build    --db "$DB"
