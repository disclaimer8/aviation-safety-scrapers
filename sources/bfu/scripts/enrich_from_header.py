#!/usr/bin/env python3
"""
scripts/enrich_from_header.py
──────────────────────────────
Enrich bea_reports metadata by parsing the standardised PDF header in
narrative_text, then rebuild bea_accidents.

For each bea_reports row with len(narrative_text) >= 80:
  1. Run parse_header(narrative_text) → {aircraft, registration, date_iso, location}
  2. For each field, set it from the header IF the header value is non-None
     (header is authoritative; if header returns None, keep the existing value).
  3. UPDATE bea_reports with the merged metadata.
  4. Reset built/skipped rows (narrative >= 80) to status='parsed'.
  5. Run pipeline.build(conn) to emit bea_accidents.
  6. Print before/after counts and metadata-completeness summary.

Usage:
    python scripts/enrich_from_header.py --db /path/to/bfu.db [--dry-run]

Self-test (no --db arg):
    python scripts/enrich_from_header.py --self-test
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bfu_ingest import db, pipeline
from bfu_ingest.header import parse_header

_NARRATIVE_FLOOR = 80  # must match pipeline._NARRATIVE_FLOOR


# ── helpers ────────────────────────────────────────────────────────────────────

def _completeness(conn):
    """Return dict of non-null counts for key columns in bea_accidents."""
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS total, "
        "  SUM(CASE WHEN event_date IS NOT NULL THEN 1 ELSE 0 END) AS has_date, "
        "  SUM(CASE WHEN registration IS NOT NULL THEN 1 ELSE 0 END) AS has_reg, "
        "  SUM(CASE WHEN aircraft IS NOT NULL THEN 1 ELSE 0 END) AS has_aircraft "
        "FROM bea_accidents"
    ).fetchone()
    # SUM() returns None on an empty table; normalise to 0
    return {k: (v or 0) for k, v in dict(row).items()}


def _status_counts(conn):
    return {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM bea_reports GROUP BY status"
        ).fetchall()
    }


# ── main enrichment ────────────────────────────────────────────────────────────

def enrich_from_header(conn, dry_run=False):
    # ── 0. baseline ────────────────────────────────────────────────────────────
    acc_before = conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0]
    comp_before = _completeness(conn)
    stat_before = _status_counts(conn)
    print(
        f"[enrich_from_header] BEFORE: bea_accidents={acc_before}, "
        f"bea_reports by status={stat_before}"
    )
    print(
        f"[enrich_from_header] BEFORE completeness (bea_accidents): "
        f"date={comp_before['has_date']}/{comp_before['total']}, "
        f"reg={comp_before['has_reg']}/{comp_before['total']}, "
        f"aircraft={comp_before['has_aircraft']}/{comp_before['total']}"
    )

    if dry_run:
        print("[enrich_from_header] DRY-RUN: no changes committed.")
        return

    # ── 1. ENRICH bea_reports from header ─────────────────────────────────────
    rows = conn.execute(
        "SELECT slug, aircraft_type, registration, date_of_occurrence, location, narrative_text "
        "FROM bea_reports "
        "WHERE length(COALESCE(narrative_text, '')) >= ?",
        (_NARRATIVE_FLOOR,),
    ).fetchall()

    enriched = 0
    for row in rows:
        h = parse_header(row["narrative_text"])

        # Merge: header wins when non-None, otherwise keep existing
        new_aircraft = h["aircraft"] if h["aircraft"] is not None else row["aircraft_type"]
        new_reg = h["registration"] if h["registration"] is not None else row["registration"]
        new_date = h["date_iso"] if h["date_iso"] is not None else row["date_of_occurrence"]
        new_loc = h["location"] if h["location"] is not None else row["location"]

        if (new_aircraft, new_reg, new_date, new_loc) != (
            row["aircraft_type"], row["registration"],
            row["date_of_occurrence"], row["location"],
        ):
            conn.execute(
                "UPDATE bea_reports SET "
                "  aircraft_type=?, registration=?, date_of_occurrence=?, location=?, "
                "  updated_at=? "
                "WHERE slug=?",
                (new_aircraft, new_reg, new_date, new_loc, db.now_ms(), row["slug"]),
            )
            enriched += 1

    conn.commit()
    print(f"[enrich_from_header] ENRICH: {enriched} bea_reports rows updated from header.")

    # ── 2. RESET built/skipped rows with full narrative → 'parsed' ────────────
    reset = conn.execute(
        "UPDATE bea_reports SET status='parsed' "
        "WHERE status IN ('built', 'skipped') "
        "  AND length(COALESCE(narrative_text, '')) >= ?",
        (_NARRATIVE_FLOOR,),
    ).rowcount
    conn.commit()
    print(f"[enrich_from_header] RESET: {reset} rows returned to 'parsed'.")

    # ── 3. BUILD ───────────────────────────────────────────────────────────────
    built = pipeline.build(conn)
    print(f"[enrich_from_header] BUILD: {built} bea_accidents rows emitted.")

    # ── 4. AFTER counts ────────────────────────────────────────────────────────
    acc_after = conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0]
    comp_after = _completeness(conn)
    stat_after = _status_counts(conn)
    print(
        f"[enrich_from_header] AFTER: bea_accidents={acc_after}, "
        f"bea_reports by status={stat_after}"
    )
    print(
        f"[enrich_from_header] AFTER completeness (bea_accidents): "
        f"date={comp_after['has_date']}/{comp_after['total']}, "
        f"reg={comp_after['has_reg']}/{comp_after['total']}, "
        f"aircraft={comp_after['has_aircraft']}/{comp_after['total']}"
    )
    print(f"[enrich_from_header] NET GAIN: +{acc_after - acc_before} accidents.")
    print(
        f"[enrich_from_header] COMPLETENESS DELTA: "
        f"date +{comp_after['has_date'] - comp_before['has_date']}, "
        f"reg +{comp_after['has_reg'] - comp_before['has_reg']}, "
        f"aircraft +{comp_after['has_aircraft'] - comp_before['has_aircraft']}"
    )


# ── self-test ─────────────────────────────────────────────────────────────────

def _run_self_test():
    """Seed an in-memory DB with two rows and verify enrichment."""
    import io
    import contextlib

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    db.init_schema(conn)

    NARRATIVE_ENGLISH = (
        "SAFETY INVESTIGATION REPORT "
        "Accident to the Robin DR300 - 140 registered F-BSPK on 24 September 2023 "
        "at Calais-Marck (Pas-de-Calais) " + "x" * 200
    )
    NARRATIVE_FRENCH = (
        "RAPPORT ACCIDENT www.bea.aero "
        "Avion Jodel D127 immatriculé F-BJJX 26 septembre 2015 vers 10 h 50 " + "x" * 200
    )

    # Row 1: English narrative, no metadata from title
    conn.execute(
        "INSERT INTO bea_reports (slug, detail_url, title, status, narrative_text, source_tier) "
        "VALUES (?,?,?,?,?,?)",
        ("s1", "http://ex.com/s1", "some title", "built", NARRATIVE_ENGLISH, "pdf"),
    )
    # Row 2: French narrative, partial metadata
    conn.execute(
        "INSERT INTO bea_reports (slug, detail_url, title, status, narrative_text, source_tier) "
        "VALUES (?,?,?,?,?,?)",
        ("s2", "http://ex.com/s2", "another title", "skipped", NARRATIVE_FRENCH, "pdf"),
    )
    conn.commit()

    # Run enrich
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        enrich_from_header(conn)
    output = buf.getvalue()

    # Verify row 1
    r1 = conn.execute("SELECT * FROM bea_reports WHERE slug='s1'").fetchone()
    assert r1["registration"] == "F-BSPK", f"s1 reg: {r1['registration']!r}"
    assert r1["date_of_occurrence"] == "2023-09-24", f"s1 date: {r1['date_of_occurrence']!r}"
    assert "Robin" in (r1["aircraft_type"] or ""), f"s1 aircraft: {r1['aircraft_type']!r}"
    assert r1["location"] is not None and "Calais" in r1["location"], f"s1 loc: {r1['location']!r}"

    # Verify row 2
    r2 = conn.execute("SELECT * FROM bea_reports WHERE slug='s2'").fetchone()
    assert r2["registration"] == "F-BJJX", f"s2 reg: {r2['registration']!r}"
    assert r2["date_of_occurrence"] == "2015-09-26", f"s2 date: {r2['date_of_occurrence']!r}"
    assert "Jodel" in (r2["aircraft_type"] or ""), f"s2 aircraft: {r2['aircraft_type']!r}"

    # Verify bea_accidents built
    acc = conn.execute("SELECT COUNT(*) FROM bea_accidents").fetchone()[0]
    assert acc == 2, f"Expected 2 accidents, got {acc}"

    print("[self-test] PASSED")
    conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="Path to bfu.db SQLite file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print baseline counts only, make no changes")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in self-test on in-memory DB and exit")
    args = parser.parse_args()

    if args.self_test:
        _run_self_test()
        return

    if not args.db:
        parser.error("--db is required (or use --self-test)")

    if not os.path.exists(args.db):
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = db.connect(args.db)
    try:
        enrich_from_header(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
