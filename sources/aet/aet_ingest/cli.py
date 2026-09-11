# aet_ingest/cli.py
import argparse
import os

from . import httpc, aet, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # httpc (vendored from _common/http.py) rather than a bare httpx.Client.
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout raised on the first attempt and truncated a run. httpc also
    # brings the robots gate and the SSRF guard, which the host copy of this
    # package predates.
    return httpc.make_client(
        headers={
            "User-Agent": aet.UA,
            "Referer": aet.REFERER,
        },
        proxy=proxy,
        timeout=60,
        delay=aet.DELAY,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aet-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="aet.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("AET_PROXY"),
        help="HTTP/SOCKS proxy URL (or set AET_PROXY env var)",
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
