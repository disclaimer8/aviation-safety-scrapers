# ntsbcarol_ingest/cli.py
"""CLI entry point for ntsbcarol-ingest.

Usage:
  ntsbcarol-ingest discover [--db PATH] [--full]
  ntsbcarol-ingest fetch [--db PATH] [--pdf-dir PATH]
  ntsbcarol-ingest parse [--db PATH]
  ntsbcarol-ingest build [--db PATH]
  ntsbcarol-ingest all [--db PATH] [--pdf-dir PATH]
  ntsbcarol-ingest count [--db PATH]   # count corpus size
  ntsbcarol-ingest verify [--db PATH]  # verify anchor cases
"""
import argparse
import sys

from . import carol, db
from .pipeline import build, discover, fetch, parse

# Anchor cases that MUST be present to verify ingest health
ANCHOR_NTSB_NOS = [
    "ANC20MA010",   # Kauai helicopter, 7 fatal
    "DCA17IA148",   # Air Canada 759, SFOR
    "DCA95MA001",   # Roselawn, 68 fatal
    "DCA24MA063",   # Alaska door plug (completed as of 2026-06)
]


def _count_corpus():
    """Return the number of completed board Aviation+Report cases in CAROL."""
    import time
    print("[ntsbcarol count] creating CAROL session...", flush=True)
    sess = carol.create_session()
    total = 0
    for prefix in carol.BOARD_PREFIXES:
        result = carol.enumerate_board_cases(sess, prefix, offset=0, page_size=1)
        n = result.get("ResultListCount", 0)
        print(f"  {prefix}: {n}", flush=True)
        total += n
        time.sleep(0.5)
    print(f"[ntsbcarol count] TOTAL corpus: {total}", flush=True)
    return total


def _verify_anchors(conn):
    """Check that all 4 anchor cases are present and have substantial narratives."""
    ok = True
    for ntsb_no in ANCHOR_NTSB_NOS:
        cid = carol.case_id(ntsb_no)
        row = conn.execute(
            "SELECT case_id, event_date, narrative_text, status FROM ntsbcarol_accidents WHERE case_id=?",
            (cid,),
        ).fetchone()
        if row is None:
            print(f"[verify] MISSING: {ntsb_no} ({cid})", flush=True)
            ok = False
        else:
            nar_len = len(row["narrative_text"] or "")
            print(
                f"[verify] OK: {ntsb_no} date={row['event_date']} "
                f"narrative={nar_len}ch status={row['status']}",
                flush=True,
            )
            if nar_len < 200:
                print(f"[verify] WARN: narrative too short for {ntsb_no}", flush=True)
                ok = False
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ntsbcarol-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "parse", "build", "all", "count", "verify"],
    )
    ap.add_argument("--db", default="ntsbcarol.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true", help="full re-enumeration (unused, parity)")
    args = ap.parse_args(argv)

    if args.mode == "count":
        _count_corpus()
        return

    conn = db.connect(args.db)
    db.init_schema(conn)

    try:
        if args.mode in ("discover", "all"):
            print(f"[ntsbcarol] discover → {args.db}", flush=True)
            print("discovered:", discover(conn, full=args.full))

        if args.mode in ("fetch", "all"):
            print(f"[ntsbcarol] fetch → {args.pdf_dir}", flush=True)
            print("fetched:", fetch(conn, args.pdf_dir))

        if args.mode in ("parse", "all"):
            print("[ntsbcarol] parse", flush=True)
            print("parsed:", parse(conn))

        if args.mode in ("build", "all"):
            print("[ntsbcarol] build", flush=True)
            print("built:", build(conn))

        if args.mode == "verify":
            _verify_anchors(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
