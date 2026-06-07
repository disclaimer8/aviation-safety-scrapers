# aaiu_ingest/cli.py
import argparse
import os
import pathlib
import ssl
import tempfile

import certifi
import httpx

from . import db, aaiu
from .pipeline import discover, fetch, build

# ⚠️ aaiu.ie serves its leaf cert WITHOUT the Sectigo DV R36 intermediate
# (verified 2026-06-04: "unable to verify the first certificate"). Browsers
# and curl recover via AIA fetching / system stores; httpx+certifi does not.
# Fix: a combined CA bundle = certifi + the pinned intermediate (shipped in
# the package; chains to the Sectigo Root R46 already in certifi).
_INTERMEDIATE = pathlib.Path(__file__).parent / "sectigo-intermediate.pem"


def _ca_bundle():
    if not _INTERMEDIATE.exists():
        return certifi.where()
    bundle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False, prefix="aaiu-ca-")
    bundle.write(open(certifi.where()).read())
    bundle.write("\n" + _INTERMEDIATE.read_text())
    bundle.close()
    return bundle.name


def _make_client(proxy=None, **_kw):
    return httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers=aaiu.HEADERS,
        proxy=proxy or None,
        verify=ssl.create_default_context(cafile=_ca_bundle()),
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="aaiu-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage to run (all = discover→fetch→build)",
    )
    ap.add_argument("--db", default="aaiu.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Cap REST pages walked (smoke runs)")
    ap.add_argument("--full", action="store_true",
                    help="Accepted for API parity; both listing pages are "
                         "always fully parsed")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("AAIU_PROXY"),
        help="SOCKS5/HTTP proxy URL (default: $AAIU_PROXY env var)",
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
