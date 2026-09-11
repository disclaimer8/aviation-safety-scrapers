#!/usr/bin/env bash
# run-cycle.sh — nightly ingest cycle for dgcakw (Kuwait DGCA).
# Run from minipc as user a1.
# The live site (kas2.dgca.gov.kw) times out; PDFs are fetched from Wayback.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$REPO_DIR/dgcakw.db"
PDF_DIR="$REPO_DIR/pdfs"

cd "$REPO_DIR"

# Activate venv if present
if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
  source "$REPO_DIR/.venv/bin/activate"
fi

echo "[dgcakw run-cycle] $(date -u +%Y-%m-%dT%H:%M:%SZ)"

python -m dgcakw_ingest.cli all --db "$DB" --pdf-dir "$PDF_DIR"

echo "[dgcakw run-cycle] done"
