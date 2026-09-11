# dgcakw_ingest/cli.py
"""CLI for dgcakw-ingest (Kuwait DGCA).

Usage:
  dgcakw-ingest discover               # populate from catalog (idempotent)
  dgcakw-ingest fetch                  # download PDFs via Wayback
  dgcakw-ingest parse                  # extract text, assess tier
  dgcakw-ingest build                  # emit dgcakw_accidents rows
  dgcakw-ingest all                    # discover + fetch + parse + build

Options:
  --db PATH         Path to SQLite DB (default: dgcakw.db in cwd)
  --pdf-dir PATH    Directory for downloaded PDFs (default: ./pdfs)
"""
import argparse
import os

from . import httpc, db
from .pipeline import discover, fetch, parse, build


def _make_client(proxy=None, **_kw):
    # httpc (vendored from _common/http.py) rather than a bare httpx.Client.
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout raised on the first attempt and truncated a run. httpc also
    # brings the robots gate and the SSRF guard.
    # 180s: this source reads through the Wayback Machine, which is slow.
    return httpc.make_client(
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
        proxy=proxy, timeout=180,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dgcakw-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default=os.path.join(os.getcwd(), "dgcakw.db"))
    ap.add_argument("--pdf-dir", default=os.path.join(os.getcwd(), "pdfs"))
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client()
    try:
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn))
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
