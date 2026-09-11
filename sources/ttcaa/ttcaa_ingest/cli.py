# ttcaa_ingest/cli.py
import argparse

from . import ttcaa, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    """Delegate to ttcaa.make_client, which builds the shared httpc client.

    The indirection exists so this package has the same shape as the other
    ~100: _common/tests/test_adoption parametrises over the VENDORED map and
    calls cli._make_client on each, which is how it proves the shared policy
    is actually reached rather than merely vendored. A package that built its
    client somewhere else would be skipped by that check while looking
    adopted.
    """
    return ttcaa.make_client(proxy=proxy)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ttcaa-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="ttcaa.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=getattr(args, 'proxy', None))
    try:
        if args.mode in ("discover", "all"):
            n = discover(conn, client, full=args.full)
            print(f"discovered: {n}")
        if args.mode in ("fetch", "all"):
            n = fetch(conn, client, args.pdf_dir)
            print(f"fetched: {n}")
        if args.mode in ("parse", "all"):
            n = parse(conn)
            print(f"parsed: {n}")
        if args.mode in ("build", "all"):
            n = build(conn)
            print(f"built: {n}")
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
