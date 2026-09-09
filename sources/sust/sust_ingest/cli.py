# sust_ingest/cli.py
import argparse
import os


from . import db, httpc, sust
from .pipeline import discover, fetch, build


def _make_client(proxy=None, **_kw):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    return httpc.make_client(headers=sust.HEADERS, proxy=proxy, timeout=120)


def _build_argparser():
    ap = argparse.ArgumentParser(prog="sust-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover->fetch->build)",
    )
    ap.add_argument("--db", default="sust.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap the number of rows fetched (smoke runs)",
    )
    ap.add_argument(
        "--proxy",
        default=os.environ.get("SUST_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $SUST_PROXY env var)",
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
            print("discovered:", discover(conn, client))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir,
                                    max_rows=args.max_rows))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
