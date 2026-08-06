# nsib_ingest/cli.py
import argparse
import os

from . import nsib, db, httpc
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    return httpc.make_client(
        headers={"User-Agent": nsib.UA, "Referer": nsib.REFERER},
        proxy=proxy,
        timeout=60,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nsib-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="nsib.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--wp-rest", action="store_true",
                    help="also enumerate WP REST API for final-report posts (HTML narratives)")
    ap.add_argument("--no-ocr", action="store_true",
                    help="skip the OCR fallback in parse (CI / non-mini-PC)")
    ap.add_argument("--proxy", default=os.environ.get("NSIB_PROXY"),
                    help="HTTP/SOCKS proxy URL (or set NSIB_PROXY env var)")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=args.proxy)
    try:
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, client, full=args.full, wp_rest=args.wp_rest))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn, enable_ocr=not args.no_ocr))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
