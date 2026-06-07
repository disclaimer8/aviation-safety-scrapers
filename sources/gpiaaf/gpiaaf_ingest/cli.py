# gpiaaf_ingest/cli.py
import argparse
import os

from . import db
from .gpiaaf import GpiaafBrowser
from .pipeline import discover, fetch, build


def _build_argparser():
    ap = argparse.ArgumentParser(
        prog="gpiaaf-ingest",
        description="GPIAAF Portugal (gpiaaf.gov.pt) civil-aviation ingest.",
    )
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "build", "all"],
        help="Pipeline stage (all = discover->fetch->build).",
    )
    ap.add_argument("--db", default="gpiaaf.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument(
        "--headed",
        action="store_true",
        default=bool(os.environ.get("GPIAAF_HEADED")),
        help="Force a headed browser (default: headless; mini-PC wraps with "
             "xvfb-run). Also via GPIAAF_HEADED env.",
    )
    ap.add_argument(
        "--full", action="store_true",
        help="API parity; all populated years are always walked.",
    )
    ap.add_argument("--max-years", type=int, default=None,
                    help="Cap year pages walked (smoke runs).")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="Cap rows kept per year page (smoke runs).")
    ap.add_argument("--max-pdfs", type=int, default=None,
                    help="Cap PDFs captured in fetch (smoke runs).")
    ap.add_argument(
        "--user-data-dir",
        default=os.environ.get("GPIAAF_USER_DATA_DIR"),
        help="Persistent Chromium profile dir (or GPIAAF_USER_DATA_DIR).",
    )
    return ap


def _parse_args(argv=None):
    return _build_argparser().parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)

    try:
        # discover AND fetch both need a browser (fetch follows the ?v= route
        # to capture the presigned S3 PDF). Open one session per invocation.
        if args.mode in ("discover", "fetch", "all"):
            with GpiaafBrowser(
                headless=not args.headed,
                user_data_dir=args.user_data_dir,
            ) as browser:
                if args.mode in ("discover", "all"):
                    print("discovered:", discover(
                        conn, browser, full=args.full,
                        max_years=args.max_years,
                        max_rows=args.max_rows,
                    ))
                if args.mode in ("fetch", "all"):
                    print("fetched:", fetch(
                        conn, browser, pdf_dir=args.pdf_dir,
                        max_pdfs=args.max_pdfs,
                    ))

        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
