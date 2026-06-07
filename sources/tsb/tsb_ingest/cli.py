# tsb_ingest/cli.py
import argparse
import os

import httpx

from . import db
from .pipeline import discover, fetch, build

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _make_client(proxy=None, **_kw):
    return httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "en-CA,en;q=0.9",
        },
        proxy=proxy or None,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="tsb-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover→fetch→build)",
    )
    ap.add_argument("--db", default="tsb.db")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; full index is always walked")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("TSB_PROXY"),
        help="SOCKS5/HTTP proxy URL, e.g. socks5h://127.0.0.1:40000 "
             "(default: $TSB_PROXY env var)",
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
            print("fetched:", fetch(conn, client))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
