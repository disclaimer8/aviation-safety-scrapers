#!/usr/bin/env bash
set -euo pipefail
cd /opt/rnsa
. .venv/bin/activate
DB=/opt/rnsa/rnsa.db
python -m rnsa_ingest.cli discover --db "$DB"
python -m rnsa_ingest.cli fetch    --db "$DB" --pdf-dir /opt/rnsa/pdfs
python -m rnsa_ingest.cli build    --db "$DB"
