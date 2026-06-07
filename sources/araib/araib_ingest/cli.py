# araib_ingest/cli.py
import argparse
import os

import certifi
import httpx

from . import araib, db
from .pipeline import discover, fetch, build


def _make_client(proxy=None, **_kw):
    # ⚠️ A PERSISTENT Client with a cookie jar is required: the first HTTPS GET
    # gets a WebtoB 307 → same URL with Set-Cookie TMOSHCooKie; follow_redirects
    # + the jar replay the handshake transparently. HTTPS only; verify via
    # certifi (the cert chain validates cleanly there).
    return httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers=araib.HEADERS,
        verify=certifi.where(),
        proxy=proxy or None,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="araib-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover->fetch->build). "
             "fetch folds in the DTL detail stage (DTL page -> PDF).",
    )
    ap.add_argument("--db", default="araib.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; the listing is always "
                         "walked in full (pages until no new rows)")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("ARAIB_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $ARAIB_PROXY env var)",
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
