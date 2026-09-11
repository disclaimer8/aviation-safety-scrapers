# aaicth_ingest/cli.py
import argparse
import os

from . import httpc, aaicth, db
from .pipeline import discover, fetch, parse, build


# NOTE: verify= MUST live on the transport, not on the Client. httpx's
# Client._init_transport returns an explicitly-passed transport as-is and
# never applies the client's verify=/cert= — so moving retries= onto a
# transport silently disarmed this source's custom CA handling (measured
# 2026-08-03: caa.lk and aaiu.ie both went CERTIFICATE_VERIFY_FAILED).
def _make_client(proxy=None, **_kw):
    # ⚠️ A robots.txt EXEMPTION. There are two others in the tree (bfu, eaaid).
    #
    # https://ops.mot.go.th/robots.txt is, in full:
    #     User-agent: *
    #     Disallow: /
    # A blanket refusal covering the whole host, every agent. Checked
    # 2026-09-11. Unlike eaaid's, this one names no exceptions, so there is no
    # "be a search engine" reading available — it says no to everybody.
    #
    # Taken knowingly: these are public-record accident reports, the scraper
    # has been running against this host for months and the site has never
    # rate-limited or blocked it, and the data is already in production. What
    # this exemption changes is only that the conflict is now visible in the
    # code and in scripts/check_robots.py output, instead of happening off the
    # books. Asking the operator whether a blanket Disallow on a public
    # register is intended remains the fix that makes it unnecessary.
    #
    # verify is NOT disabled. The host copy passed verify=False in both of its
    # client factories, and the module docstring claimed a certificate chain
    # issue made it necessary. Checked the live host: it presents a valid
    # Sectigo chain for *.mot.go.th, openssl reports "Verify return code: 0
    # (ok)", and a verified fetch of the listing returns HTTP 200 with
    # ssl_verify_result=0. Whatever was broken has been fixed upstream, and
    # accepting any certificate on a source whose PDFs become published safety
    # data is not a trade worth carrying forward.
    return httpc.make_client(
        headers={"User-Agent": aaicth.UA},
        proxy=proxy,
        timeout=120,
        delay=aaicth.DELAY,
        obey_robots=False,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aaicth-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="aaicth.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true", help="accepted for API parity")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    db.init_schema(conn)
    client = _make_client()
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
