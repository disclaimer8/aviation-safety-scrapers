# aaicth_ingest/cli.py
import argparse
import os

from . import aaicth, db
from .pipeline import discover, fetch, parse, build


# NOTE: verify= MUST live on the transport, not on the Client. httpx's
# Client._init_transport returns an explicitly-passed transport as-is and
# never applies the client's verify=/cert= — so moving retries= onto a
# transport silently disarmed this source's custom CA handling (measured
# 2026-08-03: caa.lk and aaiu.ie both went CERTIFICATE_VERIFY_FAILED).
def _make_client():
    import httpx
    return httpx.Client(transport=httpx.HTTPTransport(retries=3, verify=False), 
        headers={"User-Agent": aaicth.UA},
        follow_redirects=True,

        timeout=httpx.Timeout(10.0, read=120.0),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aaicth-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="aaicth.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true", help="accepted for API parity")
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
