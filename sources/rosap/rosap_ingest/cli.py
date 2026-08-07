# rosap_ingest/cli.py
import argparse
import sys

from . import db, rosap
from .pipeline import discover, fetch, parse, build


def _build_argparser():
    ap = argparse.ArgumentParser(prog="rosap-ingest")
    ap.add_argument(
        "mode",
        choices=["discover", "fetch", "parse", "build", "all"],
        help="Pipeline stage to run (all = discover→fetch→parse→build)",
    )
    ap.add_argument("--db", default="rosap.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Cap listing pages walked (smoke runs)")
    ap.add_argument("--no-ocr", action="store_true",
                    help="Skip OCR in parse (CI / no OCR host available)")
    return ap


def _parse_args(argv=None):
    """Parse CLI arguments; exposed for testing."""
    return _build_argparser().parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)

    # parse() and build() never touch the network, so they must not pay for a
    # browser — launching Chrome under Xvfb just to OCR local files would make
    # a re-parse impossible on a box without a display.
    needs_browser = args.mode in ("discover", "fetch", "all")

    browser = page = playwright = None
    try:
        if needs_browser:
            from patchright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=False,
                                                 args=["--no-sandbox"])
            ctx = browser.new_context(user_agent=rosap.UA, locale="en-US",
                                      accept_downloads=True)
            page = ctx.new_page()

        if args.mode in ("discover", "all"):
            print("discovered:", discover(conn, page, max_pages=args.max_pages))
        if args.mode in ("fetch", "all"):
            print("fetched:", fetch(conn, page, pdf_dir=args.pdf_dir))
        if args.mode in ("parse", "all"):
            print("parsed:", parse(conn, enable_ocr=not args.no_ocr))
        if args.mode in ("build", "all"):
            print("built:", build(conn))
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
