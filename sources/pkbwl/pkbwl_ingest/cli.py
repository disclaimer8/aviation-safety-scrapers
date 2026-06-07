# pkbwl_ingest/cli.py
import argparse
import os

import httpx

from . import db, pkbwl
from .pipeline import discover, fetch, build


def _make_client(proxy=None, **_kw):
    return httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers=pkbwl.HEADERS,
        proxy=proxy or None,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="pkbwl-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover->fetch->build)",
    )
    ap.add_argument("--db", default="pkbwl.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; the listing is always "
                         "walked page-by-page until the 404 past-the-end stop")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("PKBWL_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $PKBWL_PROXY env var)",
    )
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
            print("discovered:", discover(conn, client, full=args.full))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
