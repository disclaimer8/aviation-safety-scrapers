#!/usr/bin/env python3
"""Null out coordinates that obviously don't match the location text.

Why: ~43% of US-flagged accident rows have longitudes < 65°W absolute —
clearly wrong, the result of an upstream DMS/DDM decoder that lost the
high digit during ingest. "Atlanta, GA, United States" with lon=-8.7075
plots in the Gulf of Guinea, not Georgia.

This migration is intentionally a one-way nullify, not an attempt to
"fix" the broken value. We can't reliably reconstruct the lost digit
(was 84.4 truncated to 8.7? or to -84.7? both plausible). Showing
fewer-but-correct points is the right trade vs. showing many-but-
plotted-on-the-wrong-continent points; users have already noticed.

A follow-up migration can re-geocode broken rows from location text
using OpenFlights airports.dat (city → lat/lon lookup) — that's a
separate, larger change. This script just stops the bleeding.

Detection: location text contains a country/state hint, and lat/lon
sit outside that country's plausible bounding box. Country bboxes
include offshore territory (Alaska/Hawaii in the US, Sakhalin in
Russia) but stay tight enough to flag obvious wrongs.
"""
import sqlite3
import sys
from coord_rules import country_bbox_for, is_outside_bbox

DB = sys.argv[1] if len(sys.argv) > 1 else "accidents.db.seed"


def main() -> None:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT id, location, lat, lon
        FROM accidents
        WHERE lat IS NOT NULL AND lat != 0 AND lat != 0.000001
        """
    ).fetchall()
    print(f"[fix-coords] scanning {len(rows):,} geocoded rows…", file=sys.stderr)

    broken = []
    for rid, location, lat, lon in rows:
        bbox = country_bbox_for(location)
        if bbox and is_outside_bbox(lat, lon, bbox):
            broken.append(rid)

    print(
        f"[fix-coords] {len(broken):,} rows have lat/lon outside their "
        f"location's plausible bbox — nulling.",
        file=sys.stderr,
    )

    if broken:
        # Batch update in chunks to keep the SQL parameter count reasonable.
        CHUNK = 500
        for i in range(0, len(broken), CHUNK):
            batch = broken[i : i + CHUNK]
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                f"UPDATE accidents SET lat = NULL, lon = NULL WHERE id IN ({placeholders})",
                batch,
            )
    conn.commit()

    remaining = cur.execute(
        "SELECT COUNT(*) FROM accidents WHERE lat IS NOT NULL AND lat != 0 AND lat != 0.000001"
    ).fetchone()[0]
    print(f"[fix-coords] {remaining:,} accidents now plot on the map (was {len(rows):,}).", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()
