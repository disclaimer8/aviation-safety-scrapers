# cins_ingest/cli.py
import argparse
import os

from . import cins, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None):
    import httpx
    # Build the transport unconditionally: it used to exist only when a
    # proxy was passed, so retries= rode along only on proxied runs and
    # every ordinary run silently fell back to httpx's default transport
    # (retries=0). HTTPTransport accepts proxy=None, so one line covers both.
    transport = httpx.HTTPTransport(proxy=proxy or None, retries=3)
    return httpx.Client(
        headers={
            "User-Agent": cins.UA,
            "Referer": cins.REFERER,
        },
        follow_redirects=True,
        timeout=60.0,
        transport=transport,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cins-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="cins.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("CINS_PROXY"),
        help="HTTP/SOCKS proxy URL (or set CINS_PROXY env var)",
    )
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=args.proxy)
    try:
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, client, full=args.full))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
