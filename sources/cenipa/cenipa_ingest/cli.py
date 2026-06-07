# cenipa_ingest/cli.py
import argparse
import os

from . import db
from .cenipa import CenipaBrowser
from .pipeline import discover, fetch, parse, build


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cenipa-ingest",
        description="CENIPA (Brazil) accident report ingest pipeline.",
    )
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "parse", "build", "all"],
        help="Pipeline stage to run.",
    )
    ap.add_argument("--db", default="cenipa.db", help="Path to SQLite database.")
    ap.add_argument("--pdf-dir", default="pdfs", help="Directory for downloaded PDFs.")
    ap.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help=(
            "Run Chromium in headless mode.  "
            "Default is headed; on the mini-PC wrap with xvfb-run."
        ),
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Disable stop-on-empty-page heuristic (walk all pages).",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N listing pages (overrides last_page(); useful for smoke runs).",
    )
    ap.add_argument(
        "--user-data-dir",
        default=os.environ.get("CENIPA_USER_DATA_DIR"),
        metavar="DIR",
        help="Persistent Chromium profile directory (or set CENIPA_USER_DATA_DIR).",
    )
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)

    try:
        if args.mode in ("discover", "fetch", "all"):
            with CenipaBrowser(
                headless=args.headless,
                user_data_dir=args.user_data_dir,
            ) as browser:
                if args.mode in ("discover", "all"):
                    print(
                        "discovered:",
                        discover(conn, browser, full=args.full, max_pages=args.max_pages),
                    )
                if args.mode in ("fetch", "all"):
                    print("fetched:", fetch(conn, browser, args.pdf_dir))

        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn))

        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
