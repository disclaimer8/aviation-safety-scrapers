# dgacgt_ingest/cli.py
import argparse
import os

from . import dgacgt, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None):
    import httpx
    transport = None
    if proxy:
        transport = httpx.HTTPTransport(proxy=proxy)
    return httpx.Client(
        headers={
            "User-Agent": dgacgt.UA,
            "Referer": dgacgt.REFERER,
        },
        follow_redirects=True,
        timeout=httpx.Timeout(90.0, connect=30.0),
        transport=transport,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dgacgt-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="dgacgt.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("DGACGT_PROXY"),
        help="HTTP/SOCKS proxy URL (or set DGACGT_PROXY env var)",
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
