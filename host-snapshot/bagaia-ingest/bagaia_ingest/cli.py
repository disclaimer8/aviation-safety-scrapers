# bagaia_ingest/cli.py
"""CLI for BAGAIA ingest: seed|discover|fetch|parse|build|all."""
import argparse
import os

from . import bagaia, db
from .pipeline import seed, discover, fetch, parse, build


def _make_client(proxy=None):
    import httpx
    # Build the transport unconditionally: it used to exist only when a
    # proxy was passed, so retries= rode along only on proxied runs and
    # every ordinary run silently fell back to httpx's default transport
    # (retries=0). HTTPTransport accepts proxy=None, so one line covers both.
    transport = httpx.HTTPTransport(proxy=proxy or None, retries=3)
    return httpx.Client(
        headers=bagaia.HEADERS,
        follow_redirects=True,
        timeout=120.0,
        transport=transport,
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
