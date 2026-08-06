"""Backfill event dates on rows built before dates.py existed.

fetch() now recovers the date while it has the page in hand, but rows already
in the database were built when the only source was the title. This walks the
dateless ones, re-reads their investigation page for the start-date field, and
writes it only where the stored report text confirms it — the same two-witness
rule fetch() applies, so a row gets the same answer either way.

    python -m ovv_ingest.backfill_dates --db ovv.db            # dry run
    python -m ovv_ingest.backfill_dates --db ovv.db --apply

Read-only against the source: one GET per dateless row, paced by ovv.DELAY.
Nothing is written unless --apply is given, and a row whose date the report
does not confirm is left alone rather than filled in approximately.
"""

import argparse
import sys
import time

from . import cli, dates, db, ovv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ovv-backfill-dates")
    ap.add_argument("--db", default="ovv.db")
    ap.add_argument("--apply", action="store_true", help="write the dates")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    sql = ("SELECT case_id, detail_url, narrative_text FROM ovv_reports "
           "WHERE (date_of_occurrence IS NULL OR date_of_occurrence='') "
           "AND LENGTH(COALESCE(narrative_text,'')) > 300")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()

    client = cli._make_client()
    found, no_field, unconfirmed, failed = [], 0, 0, 0
    try:
        for row in rows:
            time.sleep(ovv.DELAY)
            try:
                html = ovv.fetch_page(client, row["detail_url"])
            except Exception as exc:
                print(f"  {row['case_id'][:48]}: page failed: {exc}", file=sys.stderr)
                failed += 1
                continue
            start = dates.parse_start_date(html)
            if not start:
                no_field += 1
                continue
            confirmed = dates.corroborated_date(start, row["narrative_text"])
            if not confirmed:
                unconfirmed += 1
                continue
            found.append((confirmed, row["case_id"]))
    finally:
        client.close()

    print(f"\ndateless rows with text: {len(rows)}")
    print(f"  confirmed by the report: {len(found)}")
    print(f"  start date not confirmed: {unconfirmed}   (left dateless on purpose)")
    print(f"  no start-date field:      {no_field}")
    print(f"  page fetch failed:        {failed}")

    if not args.apply:
        print("\ndry run — pass --apply to write")
        return 0

    update = conn.execute
    written = 0
    for iso, case_id in found:
        written += update(
            "UPDATE ovv_reports SET date_of_occurrence=?, updated_at=? "
            "WHERE case_id=? AND (date_of_occurrence IS NULL OR date_of_occurrence='')",
            (iso, db.now_ms(), case_id),
        ).rowcount
    conn.commit()
    print(f"\nwrote {written} dates — run `build` to project them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
