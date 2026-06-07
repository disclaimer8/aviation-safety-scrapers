#!/usr/bin/env python3
"""Null coordinate clusters that look like Nominatim country/region centroids.

Why: when the upstream geocoder gave Nominatim a generic country-only query
("United States", "Russia", "United Kingdom"), Nominatim returns the country
centroid — a single fixed (lat, lon) shared by every such row. The biggest
such cluster on 2026-05-13 was 94 rows all stacked on (39.7837304,
−100.445882) — the geographic center of contiguous USA, in the middle of
Kansas where no accident actually happened. Visually it looks like a fake
mass-event in a cornfield.

Heuristic — the SAFE signal:
  null a (lat, lon) cluster ONLY when EVERY row in it has a location text
  that is JUST a country/region name (no comma, no city, no airport code).

An earlier version of this script used distinct_locs ≤ 2 + count ≥ 10 to
flag clusters, but that destroyed legitimate Alaskan small-airport clusters
("Wasilla, AK, United States" × 20 is a real airport, not a centroid). Rural
GA hubs naturally have low distinct_locs because the location text format
is uniform — they're indistinguishable from centroids by that metric alone.

The country-name-only filter is conservative on purpose: we'd rather leave a
few real centroid stragglers in than null real airport coords. The accepted
country/region names are listed in COUNTRY_ONLY below; expand as new
single-word centroids are found in the wild.

After nulling, the regeocode pass picks these rows up — but only if their
location text resolves via airports.dat. A row with location "Russia" has
no city to lookup, so it stays NULL and disappears from the map. That is
correct: we have no information to pin a real coord for a generic country
row.

Idempotent — once nulled, the location text doesn't change, so subsequent
runs find the same rows already NULL and do nothing.
"""
import re
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "accidents.db.seed"

# Location texts that are bare country/region names with no city detail.
# Match against `.strip()`-normalised location, case-insensitive. Keep this
# list short — it's intentionally conservative.
COUNTRY_ONLY = {
    "united states", "usa", "u.s.a.", "u.s.",
    "russia", "russian federation",
    "canada",
    "china",
    "japan",
    "brazil",
    "mexico",
    "france",
    "germany",
    "united kingdom", "u.k.", "uk", "england",
    "australia",
    "india",
    "indonesia",
    "italy",
    "spain",
    "argentina",
    "afghanistan",
    "iran", "iraq",
    "pakistan",
    "south africa",
    "colombia",
    "venezuela",
    "ukraine",
    "philippines",
    "thailand",
    "vietnam",
}

# Also accept "unknown" and empty-ish strings that Nominatim sometimes maps
# to the geographic centroid of the *world* (around 0, 0 — but if those
# bypassed the sentinel filter and landed on a non-zero centroid, catch them
# here).
EMPTY_LIKE = {"unknown", "n/a", "-", "near"}


def is_country_only(location: str) -> bool:
    if not location:
        return True
    norm = location.strip().lower()
    if norm in COUNTRY_ONLY or norm in EMPTY_LIKE:
        return True
    # Also catch trailing-comma artefacts like "Russia," or stripped quotes.
    norm_clean = re.sub(r"[,.;]+$", "", norm).strip()
    return norm_clean in COUNTRY_ONLY


def main() -> None:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # Pull all currently-geocoded rows with their locations so we can group
    # in Python (SQL can't run the country-only predicate efficiently).
    rows = cur.execute(
        """
        SELECT lat, lon, id, location
        FROM accidents
        WHERE lat IS NOT NULL AND lat != 0 AND lat != 0.000001
        """
    ).fetchall()

    # Bucket rows by (lat, lon) and tag each bucket as "all-country-only" or not.
    buckets = {}
    for lat, lon, rid, loc in rows:
        buckets.setdefault((lat, lon), []).append((rid, loc))

    suspect_ids = []
    suspect_clusters = []
    for (lat, lon), members in buckets.items():
        if len(members) < 2:
            continue   # singletons can't be a Nominatim-centroid pattern
        if all(is_country_only(loc) for _, loc in members):
            suspect_clusters.append((lat, lon, len(members)))
            suspect_ids.extend(rid for rid, _ in members)

    if not suspect_ids:
        print("[decentroid] no country-centroid clusters found.", file=sys.stderr)
        conn.close()
        return

    print(
        f"[decentroid] found {len(suspect_clusters):,} country-centroid clusters:",
        file=sys.stderr,
    )
    for lat, lon, n in sorted(suspect_clusters, key=lambda c: -c[2])[:20]:
        sample = next(
            (loc for rid, loc in buckets[(lat, lon)] if loc),
            "",
        )
        print(
            f"[decentroid]   ({lat}, {lon}) — {n} rows — sample: {sample!r}",
            file=sys.stderr,
        )
    if len(suspect_clusters) > 20:
        print(f"[decentroid]   …and {len(suspect_clusters) - 20} more.", file=sys.stderr)

    print(f"[decentroid] nulling {len(suspect_ids):,} rows total.", file=sys.stderr)

    CHUNK = 500
    for i in range(0, len(suspect_ids), CHUNK):
        batch = suspect_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(batch))
        cur.execute(
            f"UPDATE accidents SET lat = NULL, lon = NULL WHERE id IN ({placeholders})",
            batch,
        )
    conn.commit()

    remaining = cur.execute(
        "SELECT COUNT(*) FROM accidents WHERE lat IS NOT NULL AND lat != 0 AND lat != 0.000001"
    ).fetchone()[0]
    print(
        f"[decentroid] map_data now plots {remaining:,} accidents.",
        file=sys.stderr,
    )

    conn.close()


if __name__ == "__main__":
    main()
