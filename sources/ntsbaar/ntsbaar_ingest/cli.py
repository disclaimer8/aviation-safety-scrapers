# ntsbaar_ingest/cli.py
import argparse
import os
import sys

from . import db, httpc, ntsbaar
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    return httpc.make_client(headers=ntsbaar.HEADERS, proxy=proxy, timeout=180)


def _build_argparser():
    ap = argparse.ArgumentParser(prog="ntsbaar-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="ntsbaar.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--no-ocr", action="store_true",
                    help="Skip OCR in parse (CI / no OCR host available)")
    ap.add_argument("--proxy", default=os.environ.get("NTSBAAR_PROXY"),
                    help="SOCKS5/HTTP proxy URL (or set $NTSBAAR_PROXY)")
    return ap


def _parse_args(argv=None):
    """Parse CLI arguments; exposed for testing."""
    return _build_argparser().parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=args.proxy)
    try:
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, client))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn, enable_ocr=not args.no_ocr))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
