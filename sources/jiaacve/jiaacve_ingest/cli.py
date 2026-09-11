# jiaacve_ingest/cli.py
import argparse
import os

from . import httpc, jiaacve, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # httpc (vendored from _common/http.py) rather than a bare httpx.Client:
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout raises on the first attempt and truncates a run. It also brings
    # the robots gate and the SSRF guard, which the host copy of this package
    # predates.
    #
    # delay is the source's own pacing. mppt.gob.ve publishes
    # `Crawl-delay: 3600`; the gate logs that divergence rather than adopting
    # it — 563 documents at one per hour is 23 days. See the module docstring.
    return httpc.make_client(
        headers={
            "User-Agent": jiaacve.UA,
            "Referer": jiaacve.LISTING_URL,
        },
        proxy=proxy,
        timeout=60,
        delay=jiaacve.DELAY,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jiaacve-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all", "status"])
    ap.add_argument("--db", default="jiaacve.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max PDFs to download per run (useful for smoke-testing first 30)",
    )
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)

    if args.mode == "status":
        _print_status(conn)
        conn.close()
        return

    client = _make_client()
    try:
        if args.mode in ("discover", "all"):
            n = discover(conn, client)
            print(f"discover: {n} new rows inserted")
        if args.mode in ("fetch", "all"):
            n = fetch(conn, client, args.pdf_dir, limit=args.limit)
            print(f"fetch: {n} rows attempted")
        if args.mode in ("parse", "all"):
            n = parse(conn)
            print(f"parse: {n} rows processed")
        if args.mode in ("build", "all"):
            n = build(conn)
            print(f"build: {n} rows built")
    finally:
        client.close()
        conn.close()


def _print_status(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jiaacve_reports GROUP BY status"
    ).fetchall()
    print("=== jiaacve_reports status ===")
    total = 0
    for r in rows:
        print(f"  {r['status']:10s}: {r['n']}")
        total += r['n']
    print(f"  {'TOTAL':10s}: {total}")

    acc = conn.execute("SELECT COUNT(*) AS n FROM jiaacve_accidents").fetchone()
    print(f"\n=== jiaacve_accidents ===")
    print(f"  total built: {acc['n']}")

    dated = conn.execute(
        "SELECT COUNT(*) AS n FROM jiaacve_accidents WHERE event_date IS NOT NULL"
    ).fetchone()
    print(f"  with event_date: {dated['n']}")

    ge300 = conn.execute(
        "SELECT COUNT(*) AS n FROM jiaacve_accidents WHERE length(narrative_text) >= 300"
    ).fetchone()
    print(f"  narrative >= 300 chars: {ge300['n']}")

    dups = conn.execute(
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT case_id FROM jiaacve_accidents GROUP BY case_id HAVING COUNT(*) > 1"
        ")"
    ).fetchone()
    print(f"  duplicate case_ids: {dups['n']}")

    types = conn.execute(
        "SELECT report_type, COUNT(*) AS n FROM jiaacve_accidents GROUP BY report_type ORDER BY n DESC"
    ).fetchall()
    print("\n  report_type distribution:")
    for r in types:
        print(f"    {r['report_type'] or '(none)':20s}: {r['n']}")


if __name__ == "__main__":
    main()
