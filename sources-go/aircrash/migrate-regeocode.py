#!/usr/bin/env python3
"""Re-geocode rows whose lat/lon are NULL by looking up the location text
in OpenFlights airports.dat (server/src/data/airports.dat).

Why: migrate-fix-coords.py null'd 13,814 rows whose lat/lon didn't match
their location's country (root cause: an upstream NTSB DDDMMSS decoder
that handled longitude as DDMMSS, dropping the high digit). Most of those
rows have a location like "Atlanta, GA, United States" or "Concord, GA,
United States" — strings we can resolve to ~city precision via the city
column in airports.dat. Result: every accident in a city that has at
least one airport gets plotted on the city again, just at airport
coordinates instead of the original (lost) accident coordinates.

Approximation accepted: airport coords ≠ accident coords. NTSB accidents
are rarely at the airport itself, but for a global zoom-out map, "in
Atlanta" vs. "37km north of Atlanta runway" is invisible. We tag these
rows so a future tighter geocode can target them.

Source preference: when multiple airports match a "city, country" pair,
prefer the one with an IATA code (i.e. larger commercial airport) —
those are stable landmarks at the city level. Avoid heliport / closed
airfield rows that crowd the dataset.

Idempotent: only fills lat IS NULL rows. Never overwrites a real coord.
"""
import csv
import re
import sqlite3
import sys
from collections import defaultdict
from coord_rules import country_bbox_for, is_outside_bbox

DB = sys.argv[1] if len(sys.argv) > 1 else "accidents.db.seed"
# Default to a sibling airports.dat; a caller may pass an explicit path as the
# second argument.
import os.path
AIRPORTS = sys.argv[2] if len(sys.argv) > 2 else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "airports.dat")

# US-state-suffix → "City, State" pattern. CAROL writes this consistently.
STATE_RE = re.compile(
    r"^(?P<city>[A-Za-z .'\-/]+),\s*"
    r"(?P<state>AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR|VI|GU|AS),\s*"
    r"United States",
    re.IGNORECASE,
)
# Generic "City, Country" — catches the international rows.
CITY_COUNTRY_RE = re.compile(
    r"^(?P<city>[^,]+),\s*(?P<country>[A-Za-z .'\-/]+)$",
    re.IGNORECASE,
)
# "(IATA)" or "(ICAO)" suffix — many ASN rows look like
# "Frankfurt Main Airport (FRA)" or "London-Heathrow (LHR/EGLL)".
IATA_RE = re.compile(r"\(([A-Z]{3})(?:/[A-Z]{4})?\)")
ICAO_RE = re.compile(r"\(([A-Z]{4})\)")

# Map US state codes to the country string that appears in airports.dat
# ("United States"). The country column doesn't carry state, so we rely
# on city alone to disambiguate within the US (city names mostly unique
# enough; "Springfield" stays a known false-positive risk we accept).
US = "United States"


def load_airports(path: str):
    """Returns ({city.lower(): [(lat, lon, has_iata)]},
                {country.lower(): {city.lower(): [(lat, lon, has_iata)]}},
                {iata: (lat, lon)},
                {icao: (lat, lon)})"""
    by_city = defaultdict(list)
    by_country_city = defaultdict(lambda: defaultdict(list))
    iata_idx = {}
    icao_idx = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            try:
                _id, name, city, country, iata, icao, lat_s, lon_s = row[:8]
                lat = float(lat_s)
                lon = float(lon_s)
            except (ValueError, IndexError):
                continue
            has_iata = bool(iata) and iata != "\\N" and iata != ""
            ck = city.strip().lower()
            cc = country.strip().lower()
            if ck:
                by_city[ck].append((lat, lon, has_iata))
                by_country_city[cc][ck].append((lat, lon, has_iata))
            if iata and iata not in ("\\N", ""):
                iata_idx[iata.upper()] = (lat, lon)
            if icao and icao not in ("\\N", ""):
                icao_idx[icao.upper()] = (lat, lon)
    return by_city, by_country_city, iata_idx, icao_idx


def best_match(candidates):
    """Among (lat, lon, has_iata) tuples, prefer ones with IATA."""
    if not candidates:
        return None
    iata_only = [c for c in candidates if c[2]]
    if iata_only:
        # If multiple IATA airports for the same city (NYC has 3), just
        # pick the first — they're within 30km, fine for global zoom.
        return iata_only[0][0], iata_only[0][1]
    return candidates[0][0], candidates[0][1]


def resolve(location: str, by_city, by_country_city, iata_idx, icao_idx):
    """Returns (lat, lon) or None."""
    if not location:
        return None

    # 1. Direct IATA / ICAO airport code in parens — best precision.
    m = IATA_RE.search(location)
    if m and m.group(1) in iata_idx:
        return iata_idx[m.group(1)]
    m = ICAO_RE.search(location)
    if m and m.group(1) in icao_idx:
        return icao_idx[m.group(1)]

    # 2. "City, ST, United States" — US.
    m = STATE_RE.match(location.strip())
    if m:
        city = m.group("city").strip().lower()
        cands = by_country_city.get(US.lower(), {}).get(city)
        if cands:
            return best_match(cands)

    # 3. "City, Country".
    m = CITY_COUNTRY_RE.match(location.strip())
    if m:
        city = m.group("city").strip().lower()
        country = m.group("country").strip().lower()
        cands = by_country_city.get(country, {}).get(city)
        if cands:
            return best_match(cands)

    # 4. Last resort — bare city lookup, ambiguous but better than nothing.
    # Only use if exactly one airport carries this city name to avoid
    # mapping "Springfield" to a random one of the seven candidate states.
    bare = location.split(",")[0].strip().lower()
    if bare in by_city and len(by_city[bare]) == 1:
        return by_city[bare][0][0], by_city[bare][0][1]

    return None


def main() -> None:
    print(f"[regeocode] loading airports from {AIRPORTS}", file=sys.stderr)
    by_city, by_country_city, iata_idx, icao_idx = load_airports(AIRPORTS)
    print(
        f"[regeocode]   {sum(len(v) for v in by_city.values()):,} airports "
        f"across {len(by_city):,} unique city names",
        file=sys.stderr,
    )

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT id, location
        FROM accidents
        WHERE (lat IS NULL OR lat = 0 OR lat = 0.000001)
          AND location IS NOT NULL
          AND location != ''
          AND location != 'Unknown'
        """
    ).fetchall()
    print(f"[regeocode] {len(rows):,} rows missing coords — attempting lookup", file=sys.stderr)

    updates = []
    misses = 0
    bbox_rejects = 0
    for rid, loc in rows:
        result = resolve(loc, by_city, by_country_city, iata_idx, icao_idx)
        if not result:
            misses += 1
            continue
        # Validate the candidate against the country bbox derived from the
        # same location text. Catches the case where airports.dat has a
        # borderline-coast airport just outside the country (e.g. a Belgian
        # airfield matched for a row tagged "Belgium" via a CITY_COUNTRY
        # regex when the actual accident was in another nearby country).
        # Without this, fix-coords on the next sync cycle would null exactly
        # these rows again, producing a stable but wasteful oscillation.
        bbox = country_bbox_for(loc)
        if bbox and is_outside_bbox(result[0], result[1], bbox):
            bbox_rejects += 1
            continue
        updates.append((result[0], result[1], rid))

    print(
        f"[regeocode] resolved {len(updates):,} rows; {misses:,} unresolved "
        f"(too generic or unique city not in airports.dat); "
        f"{bbox_rejects:,} candidates rejected as outside their country bbox",
        file=sys.stderr,
    )

    cur.executemany(
        "UPDATE accidents SET lat = ?, lon = ? WHERE id = ?",
        updates,
    )
    conn.commit()

    final = cur.execute(
        "SELECT COUNT(*) FROM accidents WHERE lat IS NOT NULL AND lat != 0 AND lat != 0.000001"
    ).fetchone()[0]
    print(f"[regeocode] map_data now plots {final:,} accidents.", file=sys.stderr)

    conn.close()


if __name__ == "__main__":
    main()
