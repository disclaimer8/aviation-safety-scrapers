#!/usr/bin/env bash
cd /home/a1/aaicnp-ingest
nohup setsid bash deploy/run-cycle.sh < /dev/null > backfill.log 2>&1 &
disown
sleep 1
pgrep -f "run-cycle" | head -1
