# aaiasb_ingest/cli.py
import argparse

from . import db
from .pipeline import discover, fetch, parse, build
from .harsia_pipeline import (
    discover_harsia,
    fetch_harsia,
    parse_harsia,
    build_harsia,
)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="aaiasb-ingest",
        description="AAIASB/HARSIA (Greece) accident report ingest pipeline.",
    )
    ap.add_argument(
        "mode",
        choices=[
            # aaiasb.eu pipeline (unchanged)
            "discover", "fetch", "parse", "build", "all",
            # harsia.gr pipeline (post-2023, CF-protected)
            "discover-harsia", "fetch-harsia", "parse-harsia", "build-harsia",
            "all-harsia",
        ],
        help="Pipeline stage to run.",
    )
    ap.add_argument("--db", default="aaiasb.db", help="Path to SQLite database.")
    ap.add_argument("--pdf-dir", default="pdfs", help="Directory for downloaded PDFs.")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    try:
        # ── aaiasb.eu modes (plain httpx, not CF-protected) ──────────────────
        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn))
        if args.mode in ("build", "all"):
            print("built:", build(conn))

        # ── harsia.gr modes (patchright CF transport) ─────────────────────────
        if args.mode in ("discover-harsia", "all-harsia"):
            print("harsia discovered:", discover_harsia(conn))
        if args.mode in ("fetch-harsia", "all-harsia"):
            print("harsia fetched:", fetch_harsia(conn, args.pdf_dir))
        if args.mode in ("parse-harsia", "all-harsia"):
            print("harsia parsed:", parse_harsia(conn))
        if args.mode in ("build-harsia", "all-harsia"):
            print("harsia built:", build_harsia(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
