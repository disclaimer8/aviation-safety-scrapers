# ttsb_ingest/cli.py
import argparse
import os
import ssl

import certifi
import httpx

from . import db, ttsb
from .pipeline import discover, fetch, build


def _ssl_context():
    """
    Full-verification SSL context (certifi trust store + hostname check) but
    with VERIFY_X509_STRICT cleared.

    ⚠️ TTSB's certificate chain lacks the Subject Key Identifier extension that
    Python 3.13+'s default strict X.509 verification now enforces, so the
    out-of-the-box context raises 'Missing Subject Key Identifier'. curl and
    browsers accept the chain; clearing only the strict flag keeps the trust
    chain + hostname verification intact while matching their leniency.
    """
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _make_client(proxy=None, **_kw):
    return httpx.Client(
        timeout=180,
        follow_redirects=True,
        headers=ttsb.HEADERS,
        proxy=proxy or None,
        verify=_ssl_context(),
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="ttsb-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover->fetch->build)",
    )
    ap.add_argument("--db", default="ttsb.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; the 5 list pages are "
                         "always walked in full")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("TTSB_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $TTSB_PROXY env var)",
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
