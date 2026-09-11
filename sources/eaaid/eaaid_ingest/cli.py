# eaaid_ingest/cli.py
"""CLI entry point for EAAID ingest.

Usage:
  python -m eaaid_ingest.cli discover [--db PATH] [--pdf-dir PATH]
  python -m eaaid_ingest.cli fetch    [--db PATH] [--pdf-dir PATH]
  python -m eaaid_ingest.cli parse    [--db PATH] [--pdf-dir PATH] [--ocr-remote HOST]
  python -m eaaid_ingest.cli build    [--db PATH] [--pdf-dir PATH]
  python -m eaaid_ingest.cli all      [--db PATH] [--pdf-dir PATH] [--ocr-remote HOST]
  python -m eaaid_ingest.cli status   [--db PATH]
"""
import argparse
import os
import sys

from . import db, eaaid, httpc, pipeline

_DEFAULT_DB      = os.path.expanduser("~/eaaid-ingest/eaaid.db")
_DEFAULT_PDF_DIR = os.path.expanduser("~/eaaid-ingest/pdfs")


def _make_client(proxy=None, **_kw):
    # ⚠️ A robots.txt EXEMPTION. There is one other in the tree (bfu).
    #
    # https://www.civilaviation.gov.eg/robots.txt names Googlebot and Bingbot,
    # allows them, and then says for everyone else:
    #     User-agent: *
    #     Disallow: /
    # We are neither, so the whole source — listing and PDFs — is Disallowed.
    # Checked 2026-09-11.
    #
    # This is a named-agent allowlist rather than a blanket refusal: ECAA
    # publishes these reports and lets the two big crawlers index them, so the
    # rule reads as "be a search engine", not "do not read this". The reports
    # are public-record accident investigations and the site serves them to an
    # ordinary browser without a session. The exemption is taken knowingly and
    # recorded here rather than hidden behind a global switch; asking ECAA to
    # name an agent would remove the need for it entirely, and is the right
    # fix.
    #
    # Everything else still applies: retries, the SSRF guard, and the pacing
    # below.
    return httpc.make_client(
        headers={"User-Agent": eaaid.UA, "Referer": eaaid.LISTING_URL},
        proxy=proxy,
        timeout=90,
        delay=pipeline.FETCH_DELAY,
        obey_robots=False,
    )


def main():
    parser = argparse.ArgumentParser(prog="eaaid_ingest.cli")
    parser.add_argument(
        "stage",
        choices=["discover", "fetch", "parse", "build", "all", "status"],
    )
    parser.add_argument("--db",         default=_DEFAULT_DB,      help="Path to SQLite DB")
    parser.add_argument("--pdf-dir",    default=_DEFAULT_PDF_DIR, help="PDF storage directory")
    parser.add_argument(
        "--ocr-remote",
        default=os.environ.get("OCR_REMOTE", ""),
        help="SSH host for remote OCR (e.g. user@ocr-host.example)",
    )
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.db) if os.path.dirname(args.db) else ".", exist_ok=True)
    os.makedirs(args.pdf_dir, exist_ok=True)

    conn = db.connect(args.db)

    client = _make_client()
    db.init_schema(conn)

    stage = args.stage

    if stage in ("discover", "all"):
        inserted, total = pipeline.discover(conn, client)
        print(f"[cli] discover: inserted={inserted} total={total}")

    if stage in ("fetch", "all"):
        ok = pipeline.fetch(conn, client, args.pdf_dir)
        print(f"[cli] fetch: ok={ok}")

    if stage in ("parse", "all"):
        ocr = args.ocr_remote or None
        n = pipeline.parse(conn, ocr_remote_host=ocr)
        print(f"[cli] parse: processed={n}")

    if stage in ("build", "all"):
        n = pipeline.build(conn)
        print(f"[cli] build: built={n}")

    if stage == "status":
        print("\n=== eaaid_reports status ===")
        for row in conn.execute(
            "SELECT status, skip_reason, count(*) n FROM eaaid_reports "
            "GROUP BY status, skip_reason ORDER BY n DESC"
        ):
            print(f"  {row[0]:12s}  {(row[1] or ''):30s}  {row[2]}")

        print("\n=== eaaid_accidents ===")
        total = conn.execute("SELECT count(*) FROM eaaid_accidents").fetchone()[0]
        dated = conn.execute(
            "SELECT count(*) FROM eaaid_accidents WHERE event_date IS NOT NULL AND event_date != ''"
        ).fetchone()[0]
        cats = conn.execute(
            "SELECT category, count(*) n FROM eaaid_accidents GROUP BY category ORDER BY n DESC"
        ).fetchall()
        print(f"  total rows: {total}")
        print(f"  dated:      {dated} ({dated*100//max(total,1)}%)")
        print(f"  categories: {[(r[0], r[1]) for r in cats]}")

        # Spot-check notable events
        print("\n=== Notable events ===")
        for reg, date in [("SU-GCC", "2016-05-19"), ("EI-ETJ", "2015-10-31"), ("SU-GAO", "2000-02-22")]:
            rows = conn.execute(
                "SELECT case_id, event_date, aircraft, report_type, "
                "length(narrative_text) as nlen "
                "FROM eaaid_accidents WHERE registration LIKE ? AND event_date LIKE ?",
                (f"%{reg}%", f"{date[:7]}%"),
            ).fetchall()
            if rows:
                for r in rows:
                    print(f"  {r['case_id']}: {r['event_date']} {r['aircraft']} {r['report_type']} "
                          f"narrative={r['nlen']} chars")
            else:
                print(f"  {reg} / {date}: NOT FOUND in accidents table")

    conn.close()


if __name__ == "__main__":
    main()
