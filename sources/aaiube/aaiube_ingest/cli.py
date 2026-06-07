# aaiube_ingest/cli.py
import argparse
import os

import httpx

from . import db, aaiube
from .pipeline import discover, fetch, build


def _make_client(proxy=None, **_kw):
    # mobilit.belgium.be serves a complete TLS chain (verified 2026-06-04),
    # so the default certifi bundle works — no pinned intermediate needed.
    return httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers=aaiube.HEADERS,
        proxy=proxy or None,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="aaiube-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover->fetch->build)",
    )
    ap.add_argument("--db", default="aaiube.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Accepted for API parity (single-page source)")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; the one page is always "
                         "fully parsed")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("AAIUBE_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $AAIUBE_PROXY env var)",
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
            print("discovered:", discover(conn, client, full=args.full,
                                          max_pages=args.max_pages))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
