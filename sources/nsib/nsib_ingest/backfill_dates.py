"""Fill event_date on rows built before dates.py existed.

build() dates new rows now, but the rows already in nsib_accidents were built
when date_of_occurrence was the only source and it is None for every
API-discovered record. That matters beyond this database: prod's
build-source-narratives.js upserts `event_date: r.event_date` straight from
here, so a dateless row on the mini-PC overwrites a dated row on prod at the
next sync — the same "sync wipes columns" shape that has bitten this project
twice before.

Dry by default:

    .venv/bin/python -m nsib_ingest.backfill_dates
    .venv/bin/python -m nsib_ingest.backfill_dates --apply
"""

import argparse
import sqlite3
import sys

from . import db as db_mod
from .dates import recover_event_date


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="path to nsib.db (default: package default)")
    ap.add_argument("--apply", action="store_true", help="write; otherwise report only")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    conn = db_mod.connect(args.db) if args.db else db_mod.connect("nsib.db")
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT case_id, narrative_text FROM nsib_accidents "
        "WHERE event_date IS NULL OR event_date = ''"
    ).fetchall()

    tally = {"narrative+id": 0, "id": 0, "narrative": 0, "conflict": 0, None: 0}
    writes = []
    conflicts = []
    for row in rows:
        date, basis = recover_event_date(row["case_id"], row["narrative_text"])
        if basis == "conflict":
            tally["conflict"] += 1
            conflicts.append(row["case_id"])
            continue
        tally[basis] = tally.get(basis, 0) + 1
        if date:
            writes.append((date, row["case_id"]))
            if args.verbose:
                print(f"    {row['case_id']} → {date} ({basis})")

    print(f"dateless rows: {len(rows)}")
    print(f"  recoverable: {len(writes)}")
    print(f"    prose + id agreeing: {tally.get('narrative+id', 0)}")
    print(f"    prose alone:         {tally.get('narrative', 0)}")
    print(f"    unambiguous id:      {tally.get('id', 0)}")
    print(f"  left dateless: {tally.get(None, 0)}")
    print(f"  conflicting:   {tally['conflict']}" + (f"  ({', '.join(conflicts)})" if conflicts else ""))

    if not args.apply:
        print("\ndry run — pass --apply to write")
        return 0

    with conn:
        conn.executemany(
            "UPDATE nsib_accidents SET event_date = ? WHERE case_id = ?", writes
        )
    print(f"\nwrote {len(writes)} event_date values")
    return 0


if __name__ == "__main__":
    sys.exit(main())
