# taic_ingest/cli.py
import argparse
import os


from . import db, httpc, taic
from .pipeline import discover, fetch, build


def _make_client(proxy=None, **_kw):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    return httpc.make_client(
        headers=taic.HEADERS,
        proxy=proxy,
        timeout=60,
        cookies=taic.COOKIES,  # big_pipe_nojs — see taic.py
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="taic-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover→fetch→build)",
    )
    ap.add_argument("--db", default="taic.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Cap listing pages walked (smoke runs)")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; listing is always walked "
                         "to the first empty page")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("TAIC_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $TAIC_PROXY env var)",
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
