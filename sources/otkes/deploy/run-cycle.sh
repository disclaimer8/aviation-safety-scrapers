#!/usr/bin/env bash
set -euo pipefail
cd /opt/otkes
. .venv/bin/activate
DB=/opt/otkes/otkes.db
PDFS=/opt/otkes/pdfs
# discover renders JS-injected listings + detail pages -> Chromium under Xvfb.
# (No anti-bot; headless would work, but we run headed/Xvfb for parity with
#  our other browser sources and to stay resilient if a block ever appears.)
xvfb-run -a python -m otkes_ingest.cli discover --db "$DB" --headed
# fetch downloads report PDFs over plain httpx (no browser needed).
python -m otkes_ingest.cli fetch --db "$DB" --pdf-dir "$PDFS"
python -m otkes_ingest.cli build --db "$DB"
