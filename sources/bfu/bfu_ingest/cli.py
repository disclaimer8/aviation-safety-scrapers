# bfu_ingest/cli.py
import argparse
import os


from . import db, httpc
from .pipeline import discover, fetch, parse, build

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _make_client(proxy=None, **_kw):
    # The retry policy lives in httpc (vendored from _common/http.py):
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout used to raise on the first attempt and truncate a run.
    # ⚠️ THE ONE robots.txt EXEMPTION IN THE TREE. Do not copy it.
    #
    # bfu-web.de's robots.txt is:
    #     Disallow: /SiteGlobals/    (plus /DE/Service/, /EN/Service/)
    #     Crawl-delay: 30
    # and discover() enumerates reports through
    #     /SiteGlobals/Forms/Suche/Untersuchungsberichtesuche_Formular.html
    # so the whole source is Disallowed. Everything else BFU publishes — the
    # report PDFs, and the official OpenData CSVs under
    # /DE/Publikationen/OpenData/ — is explicitly allowed. The prohibition is
    # aimed at BFU's search UI, which is exactly what we drive.
    #
    # This is a stopgap, not a position. The right fix is to enumerate from
    # the allowed OpenData files instead of the search form; until that lands,
    # this keeps a working source alive rather than pretending the conflict is
    # not there. It is the single obey_robots=False in the repository, and CI
    # asserts that (see tests/test_robots_exemption.py).
    #
    # Checked 2026-09-11 against the live robots.txt of all 39 packages that
    # vendor the guard: BFU is the only one affected.
    return httpc.make_client(
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
        proxy=proxy,
        timeout=60,
        obey_robots=False,
    )


def _build_argparser():
    ap = argparse.ArgumentParser(prog="bfu-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="bfu.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("BFU_PROXY"),
        help="SOCKS5/HTTP proxy URL, e.g. socks5h://127.0.0.1:40000 "
             "(default: $BFU_PROXY env var)",
    )
    return ap


def _parse_args(argv=None):
    """Parse CLI arguments; exposed for testing."""
    return _build_argparser().parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client(proxy=args.proxy)
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
