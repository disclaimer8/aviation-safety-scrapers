# bea_ingest/cli.py
import argparse


from . import db, httpc
from .pipeline import discover, refetch, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    return httpc.make_client(headers={"User-Agent": "bea-ingest/1.0"},
                             proxy=proxy, timeout=60)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bea-ingest")
    ap.add_argument("mode", choices=["discover", "refetch", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="bea.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client()
    try:
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, client, full=args.full))
        if args.mode in ("refetch", "all"):
            # Before fetch on purpose: re-queued stubs are re-resolved by the
            # same cycle's fetch pass.
            print("refetched:", refetch(conn))
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
