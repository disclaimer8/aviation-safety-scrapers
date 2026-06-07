# otkes_ingest/cli.py
import argparse
import os

import httpx

from . import db, otkes
from .otkes import OtkesBrowser
from .pipeline import discover, fetch, build


def _make_client():
    return httpx.Client(
        timeout=180,
        follow_redirects=True,
        headers=otkes.HEADERS,
        verify=True,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(
        prog="otkes-ingest",
        description="OTKES Finland (turvallisuustutkinta.fi) aviation ingest.",
    )
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage (all = discover->fetch->build).",
    )
    ap.add_argument("--db", default="otkes.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument(
        "--headed",
        action="store_true",
        default=bool(os.environ.get("OTKES_HEADED")),
        help="Force a headed browser (default: headless; mini-PC wraps with "
             "xvfb-run). Also via OTKES_HEADED env.",
    )
    ap.add_argument(
        "--full", action="store_true",
        help="API parity; all listings are always walked.",
    )
    ap.add_argument("--max-listings", type=int, default=None,
                    help="Cap listings walked (smoke runs).")
    ap.add_argument("--max-details", type=int, default=None,
                    help="Cap new detail pages rendered (smoke runs).")
    ap.add_argument(
        "--user-data-dir",
        default=os.environ.get("OTKES_USER_DATA_DIR"),
        help="Persistent Chromium profile dir (or OTKES_USER_DATA_DIR).",
    )
    return ap


def _parse_args(argv=None):
    return _build_argparser().parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)

    try:
        if args.mode in ("discover", "all"):
            with OtkesBrowser(
                headless=not args.headed,
                user_data_dir=args.user_data_dir,
            ) as browser:
                print("discovered:", discover(
                    conn, browser, full=args.full,
                    max_listings=args.max_listings,
                    max_details=args.max_details,
                ))

        if args.mode in ("fetch", "all"):
            client = _make_client()
            try:
                print("fetched:", fetch(conn, client, pdf_dir=args.pdf_dir))
            finally:
                client.close()

        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
