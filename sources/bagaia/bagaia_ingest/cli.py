# bagaia_ingest/cli.py
"""CLI for BAGAIA ingest: seed|discover|fetch|parse|build|all."""
import argparse
import os

from . import httpc, bagaia, db
from .pipeline import seed, discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # httpc (vendored from _common/http.py) rather than a bare httpx.Client.
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout raised on the first attempt and truncated a run. httpc also
    # brings the robots gate and the SSRF guard.
    return httpc.make_client(
        headers=bagaia.HEADERS, proxy=proxy, timeout=120, delay=bagaia.DELAY,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bagaia-ingest")
    ap.add_argument(
        "mode",
        choices=["seed", "discover", "fetch", "parse", "build", "all"],
    )
    ap.add_argument("--db", default="bagaia.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("BAGAIA_PROXY"),
        help="HTTP/SOCKS proxy (or $BAGAIA_PROXY)",
    )
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=args.proxy)
    try:
        if args.mode in ("seed", "all"):
            print("seeded:", seed(conn))
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, client))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
        n = conn.execute(
            "SELECT COUNT(*) FROM bagaia_accidents"
        ).fetchone()[0]
        print("accidents total:", n)
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
