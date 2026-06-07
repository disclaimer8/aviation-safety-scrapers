#!/usr/bin/env python3
"""Add canonical (deduplication) columns + display lookup tables.

Why: NTSB / ASN / Wikidata write aircraft_model and operator as free-text,
so the same physical aircraft / airline appears under many spellings:

  "BOEING 737 800"     | 137
  "BOEING 737-800"     |  28
  "Boeing 737-800"     |  ~5
  "Delta Air Lines"    | 107
  "DELTA AIR LINES INC"|  58
  "Delta Air Lines, Inc."| 7
  "Delta Airlines"     |   7
  ...

Top-10 stats see all of these as separate buckets, splitting the count and
crowding the rankings. We add aircraft_canonical / operator_canonical
columns populated by a simple normaliser, plus per-group lookup tables
mapping canonical → most-common original spelling (so the API still shows
a human-readable name).

Normalisation rules (lossy on purpose — we WANT to merge near-duplicates):
  Aircraft:
    - UPPER, trim
    - strip parenthetical content "(WL)", "(LR)" etc.
    - strip noise tokens: WL, PASSENGER, PAX, NO SERIES
    - replace - / _ with space
    - collapse whitespace
  Operator:
    - UPPER, trim
    - strip parens
    - strip trailing/internal company suffixes: INC, INC., LLC, LTD,
      LIMITED, CORP, CORPORATION, CO., GMBH, PLC, S.A., S.L.
    - collapse "AIR LINES" → "AIRLINES" (Delta variant)
    - collapse whitespace
"""
import re
import sqlite3
import sys
from collections import Counter, defaultdict

DB = sys.argv[1] if len(sys.argv) > 1 else "accidents.db.seed"

NOISE_AIRCRAFT = re.compile(r"\b(WL|PASSENGER|PAX|NO SERIES|SERIES)\b", re.IGNORECASE)
COMPANY_SUFFIX = re.compile(
    r"[,]?\s+("
    r"INC\.?|INCORPORATED|"
    r"LLC|"
    r"LTD\.?|LIMITED|"
    r"CORP\.?|CORPORATION|"
    r"CO\.?|COMPANY|"
    r"GMBH|"
    r"S\.A\.?|S\.L\.?|"
    r"PLC|"
    r"AB|AS|OY"
    r")\.?\b",
    re.IGNORECASE,
)


def canon_aircraft(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    s = re.sub(r"\([^)]*\)", "", s)
    s = NOISE_AIRCRAFT.sub("", s)
    s = re.sub(r"[-_/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canon_operator(s: str) -> str:
    if not s:
        return ""
    s = s.upper().strip()
    s = re.sub(r"\([^)]*\)", "", s)
    # Strip trailing punctuation BEFORE the suffix sub so "INC." → "INC"
    # — otherwise the \b after \.? fails at end-of-string (the trailing
    # "." is \W, end-of-string is \W, no \w-to-\W transition exists).
    s = re.sub(r"[.,;]+\s*$", "", s)
    # Strip company suffixes — apply twice to catch "Co., Inc." and "Air Lines, Inc."
    for _ in range(2):
        s = COMPANY_SUFFIX.sub("", s)
        s = re.sub(r"[.,;]+\s*$", "", s)
    s = re.sub(r"\bAIR\s+LINES\b", "AIRLINES", s)
    s = re.sub(r"[.,;]+\s*$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main() -> None:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cols = {row[1] for row in cur.execute("PRAGMA table_info(accidents)")}
    if "aircraft_canonical" not in cols:
        cur.execute("ALTER TABLE accidents ADD COLUMN aircraft_canonical TEXT")
        print("[migrate-canonical] added aircraft_canonical column", file=sys.stderr)
    if "operator_canonical" not in cols:
        cur.execute("ALTER TABLE accidents ADD COLUMN operator_canonical TEXT")
        print("[migrate-canonical] added operator_canonical column", file=sys.stderr)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS aircraft_display (
            canonical TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS operator_display (
            canonical TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
        )
        """
    )

    rows = cur.execute(
        "SELECT id, aircraft_model, operator FROM accidents"
    ).fetchall()
    print(f"[migrate-canonical] processing {len(rows)} rows…", file=sys.stderr)

    aircraft_groups: dict[str, Counter] = defaultdict(Counter)
    operator_groups: dict[str, Counter] = defaultdict(Counter)
    updates = []

    for rid, model, op in rows:
        ac = canon_aircraft(model or "")
        oc = canon_operator(op or "")
        updates.append((ac, oc, rid))
        if ac and model:
            aircraft_groups[ac][model.strip()] += 1
        if oc and op:
            operator_groups[oc][op.strip()] += 1

    cur.executemany(
        "UPDATE accidents SET aircraft_canonical = ?, operator_canonical = ? WHERE id = ?",
        updates,
    )

    # display = most common original spelling per canonical group
    aircraft_display = [
        (canon, g.most_common(1)[0][0]) for canon, g in aircraft_groups.items()
    ]
    operator_display = [
        (canon, g.most_common(1)[0][0]) for canon, g in operator_groups.items()
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO aircraft_display (canonical, display_name) VALUES (?, ?)",
        aircraft_display,
    )
    cur.executemany(
        "INSERT OR REPLACE INTO operator_display (canonical, display_name) VALUES (?, ?)",
        operator_display,
    )

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_accidents_aircraft_canonical ON accidents(aircraft_canonical)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_accidents_operator_canonical ON accidents(operator_canonical)"
    )

    conn.commit()

    # Stats: how much consolidation did we get?
    raw_aircraft = cur.execute(
        "SELECT COUNT(DISTINCT aircraft_model) FROM accidents WHERE aircraft_model IS NOT NULL AND aircraft_model != ''"
    ).fetchone()[0]
    canon_aircraft_count = cur.execute(
        "SELECT COUNT(DISTINCT aircraft_canonical) FROM accidents WHERE aircraft_canonical IS NOT NULL AND aircraft_canonical != ''"
    ).fetchone()[0]
    raw_op = cur.execute(
        "SELECT COUNT(DISTINCT operator) FROM accidents WHERE operator IS NOT NULL AND operator != ''"
    ).fetchone()[0]
    canon_op_count = cur.execute(
        "SELECT COUNT(DISTINCT operator_canonical) FROM accidents WHERE operator_canonical IS NOT NULL AND operator_canonical != ''"
    ).fetchone()[0]

    print(
        f"\n[migrate-canonical] aircraft: {raw_aircraft:,} unique → {canon_aircraft_count:,} canonical "
        f"({raw_aircraft - canon_aircraft_count:,} merged)",
        file=sys.stderr,
    )
    print(
        f"[migrate-canonical] operator: {raw_op:,} unique → {canon_op_count:,} canonical "
        f"({raw_op - canon_op_count:,} merged)",
        file=sys.stderr,
    )

    conn.close()


if __name__ == "__main__":
    main()
