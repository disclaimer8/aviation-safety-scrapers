#!/usr/bin/env python3
"""Ask every source's OWN robots.txt about its OWN entry URL and User-Agent.

This exists because turning the robots guard on broke BFU, and it broke it
after a check that looked fine. The check was wrong: it used the listing URL
and the UA `bfu-ingest/1.0`, while the scraper actually drives the search form
under /SiteGlobals/ with a browser UA. Both halves have to come from the
package itself or the answer means nothing.

Network access required, so this is NOT a CI gate — CI runs with sockets
blocked. Run it before changing anything about robots handling, and after a
source changes its entry URL or User-Agent:

    python scripts/check_robots.py
    python scripts/check_robots.py --only bfu,bea
"""
import argparse
import importlib
import pathlib
import sys
import urllib.robotparser
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"

# The constants a package uses to name where its walk starts, most specific
# first: several define more than one and the earlier ones are the real entry.
# Sources that knowingly run with obey_robots=False. Kept in step with
# _common/tests/test_robots_exemption.py, which fails if a new one appears.
EXEMPT = {"bfu"}

ENTRY_CONSTANTS = ("SEARCH", "SEARCH_URL", "LISTING_URL", "LISTING", "INDEX_URL",
                   "MAIN_URL", "LANDING", "HUB_URL", "LIST_URL", "REPORTS_URL",
                   "EVENTS_BASE", "MANIFEST_URL", "BASE_URL", "START_URL", "BASE")


def entry_points(code):
    """(user_agent, url) taken from the package itself, not from a guess."""
    pkg_dir = SOURCES / code
    sys.path.insert(0, str(pkg_dir))
    try:
        cli = importlib.import_module(f"{code}_ingest.cli")
        client = cli._make_client()
        try:
            ua = client.headers.get("User-Agent", "")
        finally:
            client.close()
        # The site module is usually <code>.py, but not always — aaib keeps its
        # endpoints in govuk.py. Try the conventional name, then every other
        # module in the package.
        names = [f"{code}_ingest.{code}"] + [
            f"{code}_ingest.{p.stem}"
            for p in sorted((pkg_dir / f"{code}_ingest").glob("*.py"))
            if p.stem not in {"__init__", "cli", "db", "httpc", "robots", "pdf", "text"}
        ]
        for module_name in names:
            try:
                mod = importlib.import_module(module_name)
            except ModuleNotFoundError:
                continue
            for name in ENTRY_CONSTANTS:
                value = getattr(mod, name, None)
                if isinstance(value, str) and value.startswith("http"):
                    return ua, value
        return ua, None
    finally:
        sys.path.remove(str(pkg_dir))
        for loaded in [m for m in sys.modules if m.startswith(f"{code}_ingest")]:
            del sys.modules[loaded]


def main():
    import httpx
    import certifi

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated source codes")
    args = ap.parse_args()

    codes = [d.name for d in sorted(SOURCES.iterdir())
             if (d / f"{d.name}_ingest" / "robots.py").exists()]
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        codes = [c for c in codes if c in wanted]

    cache, blocked, skipped = {}, [], []
    for code in codes:
        try:
            ua, url = entry_points(code)
        except Exception as exc:
            skipped.append((code, f"{type(exc).__name__}: {exc}"))
            continue
        if not url:
            skipped.append((code, "no entry URL constant found"))
            continue

        parts = urlparse(url)
        key = (parts.scheme, parts.netloc)
        if key not in cache:
            try:
                resp = httpx.get(f"{parts.scheme}://{parts.netloc}/robots.txt",
                                 timeout=25, follow_redirects=True,
                                 verify=certifi.where(), headers={"User-Agent": ua})
                if resp.status_code >= 400:
                    cache[key] = None
                else:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    cache[key] = parser
            except Exception:
                cache[key] = None
        parser = cache[key]

        if parser is None:
            print(f"  ok        {code:12} (no robots.txt)")
        elif parser.can_fetch(ua, url):
            delay = parser.crawl_delay(ua) or parser.crawl_delay("*")
            note = f"  crawl-delay {delay}s" if delay else ""
            print(f"  ok        {code:12}{note}")
        elif code in EXEMPT:
            print(f"  exempt    {code:12} Disallowed, and knowingly so "
                  f"(obey_robots=False)")
        else:
            blocked.append((code, url, ua))
            print(f"  BLOCKED   {code:12} {url}")
            print(f"            {'':12} UA={ua}")

    for code, why in skipped:
        print(f"  skipped   {code:12} {why}")

    print(f"\nchecked {len(codes)}  blocked {len(blocked)}  skipped {len(skipped)}")
    if blocked:
        print("\nA blocked source either needs an allowed route to the same data,\n"
              "or a documented obey_robots=False (see sources/bfu/bfu_ingest/cli.py\n"
              "and _common/tests/test_robots_exemption.py).")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
