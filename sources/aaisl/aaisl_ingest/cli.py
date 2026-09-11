# aaisl_ingest/cli.py
import argparse
import os

from . import httpc, aaisl, db
from .pipeline import discover, fetch, parse, build


# NOTE: verify= MUST live on the transport, not on the Client. httpx's
# Client._init_transport returns an explicitly-passed transport as-is and
# never applies the client's verify=/cert= — so moving retries= onto a
# transport silently disarmed this source's custom CA handling (measured
# 2026-08-03: caa.lk and aaiu.ie both went CERTIFICATE_VERIFY_FAILED).
def _make_client(proxy=None, **_kw):
    # httpc (vendored from _common/http.py) rather than a bare httpx.Client.
    # httpx's own retries= covers connect errors only, so a 502 or a read
    # timeout raised on the first attempt and truncated a run. httpc also
    # brings the robots gate and the SSRF guard.
    #
    # verify= carries the pinned CAA-LK chain through. httpx ignores verify=
    # when a transport is supplied, which is why httpc takes it as a named
    # argument and applies it to the inner transport itself — passing it to
    # httpx.Client here would silently fall back to the system store and the
    # handshake would fail.
    return httpc.make_client(
        headers={"User-Agent": aaisl.UA, "Referer": aaisl.REFERER},
        proxy=proxy, timeout=60, delay=aaisl.DELAY,
        verify=aaisl.ca_bundle(),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aaisl-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="aaisl.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--proxy",
        default=os.environ.get("AAISL_PROXY"),
        help="HTTP/SOCKS proxy URL (or set AAISL_PROXY env var)",
    )
    args = ap.parse_args(argv)

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
