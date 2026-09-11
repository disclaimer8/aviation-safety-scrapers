#!/usr/bin/env bash
# One-off B3A full back-catalogue re-scrape to capture the Probable cause
# blocks (parser gained field--name-field-crash-causes on 2026-06-11).
# Cycle = seed-from-prod -> full scrape -> sync-to-prod; afterwards mark
# every prod article whose narrative gained causes as stale so the home
# LLM worker regenerates its Findings from the official conclusions.
set -euo pipefail
cd /home/a1/flightfinder/server
export BAAA_INGEST_ARGS="--full --from=1916-01-01"
bash scripts/baaa-cycle.sh
echo "[backfill] cycle done — invalidating prod articles whose narrative gained causes"
ssh user@prod.example "sqlite3 /var/lib/flightfinder/data/app.db \"UPDATE accident_articles SET status='stale' WHERE source='baaa' AND status='ok' AND EXISTS (SELECT 1 FROM accident_narratives n WHERE n.source='baaa' AND n.source_event_id = accident_articles.case_id AND n.narrative_text LIKE '%Probable cause (official findings)%'); SELECT changes();\""
echo "[backfill] DONE $(date -u +%FT%TZ)"
