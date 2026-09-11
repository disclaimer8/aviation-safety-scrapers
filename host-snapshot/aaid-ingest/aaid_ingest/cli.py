# aaid_ingest/cli.py
import argparse

from . import aaid, db
from .pipeline import discover, fetch, parse, build


def _make_client():
    """
    AAID client pinned to the captured (expired) cert chain.

    Uses aaid.make_client() which builds an SSLContext pinned to
    aaid_ca_bundle.pem with ONLY the expiry check disabled.  Pin failure raises
    loudly; there is NO insecure fallback.
    """
    return aaid.make_client()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aaid-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="aaid.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client()
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
