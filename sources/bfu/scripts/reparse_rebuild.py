#!/usr/bin/env python3
"""
scripts/reparse_rebuild.py
──────────────────────────
One-shot migration: re-parse stored titles and rebuild bea_accidents without
re-downloading any PDFs.

Usage:
    python scripts/reparse_rebuild.py --db /path/to/bfu.db [--dry-run]

What it does (three steps):
  1. REPARSE — for every row in bea_reports, re-run parse_event_title(title)
     and UPDATE the five metadata columns
     (event_class / aircraft_type / registration / date_of_occurrence /
      location / operator).
     Rows with a NULL or empty title are left unchanged.

  2. RESET — rows whose status is 'built' or 'skipped' AND whose
     narrative_text has length >= 80 chars are reset to status='parsed'
     so that build() can process them.  Rows with short / empty narratives
     are intentionally NOT reset (they would just be skipped again).

  3. BUILD — call pipeline.build(conn) to emit bea_accidents records for
     all status='parsed' rows.

Motivation:
  The full BEA backfill produced 6 152 events but only 729 bea_accidents
  because (a) parse_event_title was too rigid (2-digit years, "identified",
  multi-aircraft, "on the <loc>" prefix all returned all-null) and (b)
  build() skipped rows with null registration+aircraft even when they had
  full 20K-60K-char French narratives.  Both bugs are fixed in bfu_ingest
  >= this commit.  Running this script recovers ~2 209 previously-skipped
  rows on the mini-PC's existing bfu.db without re-downloading PDFs.
"""
import argparse
import sys
import os

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bfu_ingest import db, pipeline
from bfu_ingest.text import parse_event_title

_NARRATIVE_FLOOR = 80  # must match pipeline._NARRATIVE_FLOOR


def reparse_rebuild(conn, dry_run=False):
    # ── 0. baseline counts ────────────────────────────────────────────────────
    def _counts():
        accidents = conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0]
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM bea_reports GROUP BY status"
            ).fetchall()
        }
        return accidents, by_status

    acc_before, status_before = _counts()
    print(f"[reparse_rebuild] BEFORE: bea_accidents={acc_before}, bea_reports by status={status_before}")

    if dry_run:
        print("[reparse_rebuild] DRY-RUN: no changes committed.")
        return

    # ── 1. REPARSE titles ─────────────────────────────────────────────────────
    rows = conn.execute("SELECT slug, title FROM bea_reports").fetchall()
    reparsed = 0
    for row in rows:
        if not row["title"]:
            continue
        parsed = parse_event_title(row["title"])
        # Only update if we got at least something (avoid clobbering existing
        # metadata with all-null when the title is genuinely unrecognised)
        if any(v is not None for v in parsed.values()):
            conn.execute(
                "UPDATE bea_reports SET "
                "  event_class=?, aircraft_type=?, registration=?, "
                "  date_of_occurrence=?, location=?, operator=? "
                "WHERE slug=?",
                (
                    parsed["event_class"],
                    parsed["aircraft_type"],
                    parsed["registration"],
                    parsed["date_iso"],
                    parsed["location"],
                    parsed["operator"],
                    row["slug"],
                ),
            )
            reparsed += 1
    conn.commit()
    print(f"[reparse_rebuild] REPARSE: {reparsed} rows updated.")

    # ── 2. RESET built/skipped rows that have a full narrative ────────────────
    reset = conn.execute(
        "UPDATE bea_reports SET status='parsed' "
        "WHERE status IN ('built', 'skipped') "
        "  AND length(COALESCE(narrative_text, '')) >= ?",
        (_NARRATIVE_FLOOR,),
    ).rowcount
    conn.commit()
    print(f"[reparse_rebuild] RESET: {reset} rows returned to 'parsed'.")

    # ── 3. BUILD ──────────────────────────────────────────────────────────────
    built = pipeline.build(conn)
    print(f"[reparse_rebuild] BUILD: {built} new bea_accidents rows emitted.")

    acc_after, status_after = _counts()
    print(f"[reparse_rebuild] AFTER:  bea_accidents={acc_after}, bea_reports by status={status_after}")
    print(f"[reparse_rebuild] NET GAIN: +{acc_after - acc_before} accidents.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="Path to bfu.db SQLite file")
    parser.add_argument("--dry-run", action="store_true", help="Print baseline counts only, make no changes")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = db.connect(args.db)
    try:
        reparse_rebuild(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
