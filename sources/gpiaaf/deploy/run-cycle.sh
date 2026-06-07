#!/usr/bin/env bash
set -euo pipefail
cd /opt/gpiaaf
. .venv/bin/activate
DB=/opt/gpiaaf/gpiaaf.db
PDFS=/opt/gpiaaf/pdfs
# FULL-BROWSER source: BOTH discover (render Nuxt SPA year tables) and fetch
# (follow the ?v= route -> capture the presigned-S3 PDF within its 60 s expiry)
# drive Chromium. We run headed under Xvfb for parity with our other browser
# sources and resilience if a fingerprint block ever appears (none observed —
# headless works on a normal box).
xvfb-run -a python -m gpiaaf_ingest.cli discover --db "$DB" --headed
xvfb-run -a python -m gpiaaf_ingest.cli fetch --db "$DB" --pdf-dir "$PDFS" --headed
python -m gpiaaf_ingest.cli build --db "$DB"
