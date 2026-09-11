#!/usr/bin/env python3
"""Reconcile the scraper inventory against the control plane's coverage seed.

These two describe the same thing and had drifted badly. The seed said
Argentina, Brazil, Macao, Seychelles and Singapore were `unknown` — never
assessed — while working scrapers for all five sat in sources/. It said Bolivia,
DR Congo, Honduras, Libya, Mozambique, Paraguay and Uzbekistan had
`no_public_archive` while scrapers for all seven were running. Thirty-four of
the eighty-five countries with a scraper were mis-marked.

That matters beyond tidiness: the coverage status is what decides where to look
for new sources next. Researching a country you already scrape is wasted work,
and so is skipping one because a five-year-old note says there is nothing there.

Three kinds of mismatch are reported:

  STALE       a scraper exists, the seed does not say direct_public_archive
  ORPHANED    the seed says direct_public_archive, no scraper anywhere
  UNDECLARED  a source with no COUNTRY_ISO2, so it cannot be reconciled at all

Usage:
    python scripts/check_coverage.py --db path/to/coverage.db
    python scripts/check_coverage.py --db ... --check     # exit 1 on any mismatch

Build the database first with:
    cd control-plane && go run ./cmd/aviation-coverage migrate --db /tmp/cov.db
                     && go run ./cmd/aviation-coverage seed    --db /tmp/cov.db
"""
import argparse
import ast
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
_COUNTRY = re.compile(r'^COUNTRY_ISO2\s*=\s*["\']([A-Z]{2})["\']', re.M)

# Sources outside sources/, which declare no constant of their own.
OTHER_TREES = {
    "sources-node/atsb": "AU",
    "sources-node/mak": "RU",
    "sources-node/ntsb": "US",
    # sources-node/wikidata and sources-go/aircrash are global, not per-country.
}


def declared_countries():
    """{iso2: [source codes]} across every tree that scrapes a country."""
    found = {}
    for d in sorted((ROOT / "sources").iterdir()):
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if "tests" in f.parts:
                continue
            m = _COUNTRY.search(f.read_text(encoding="utf-8", errors="replace"))
            if m:
                found.setdefault(m.group(1), []).append(d.name)
                break
        else:
            found.setdefault(None, []).append(d.name)
    for path, iso2 in OTHER_TREES.items():
        found.setdefault(iso2, []).append(path)
    return found


def archived_countries():
    """Countries covered only by host-snapshot/, which is not built from here.

    They count as 'we have the code' for reconciliation, but not as coverage —
    the distinction the report draws.
    """
    found = {}
    for d in sorted((ROOT / "host-snapshot").glob("*-ingest")):
        code = d.name.replace("-ingest", "")
        for f in sorted(d.rglob("*.py")):
            if "tests" in f.parts:
                continue
            try:
                doc = ast.get_docstring(ast.parse(
                    f.read_text(encoding="utf-8", errors="replace"))) or ""
            except SyntaxError:
                continue
            m = re.search(r"country[ =:]+([A-Z]{2})\b", doc)
            if m:
                found.setdefault(m.group(1), []).append(code)
                break
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="a migrated+seeded coverage database")
    ap.add_argument("--check", action="store_true", help="exit 1 on any mismatch")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cov = {r["iso2"]: (r["name"], r["coverage_status"])
           for r in conn.execute("SELECT iso2, name, coverage_status FROM countries")}
    if not cov:
        print("the coverage database is empty — run migrate and seed first",
              file=sys.stderr)
        return 2

    live = declared_countries()
    undeclared = sorted(live.pop(None, []))
    archived = archived_countries()

    stale, orphaned, unknown_iso = [], [], []
    for iso2, codes in sorted(live.items()):
        if iso2 not in cov:
            unknown_iso.append((iso2, codes))
            continue
        name, status = cov[iso2]
        if status != "direct_public_archive":
            stale.append((iso2, name, status, codes))

    covered = set(live) | set(archived)
    for iso2, (name, status) in sorted(cov.items()):
        if status == "direct_public_archive" and iso2 not in covered:
            orphaned.append((iso2, name))

    print(f"countries in the seed:            {len(cov)}")
    print(f"countries with a live scraper:    {len(live)}")
    print(f"countries only in host-snapshot:  {len(set(archived) - set(live))}")
    print()

    print(f"STALE — a scraper exists, the seed disagrees: {len(stale)}")
    for iso2, name, status, codes in stale:
        print(f"  {iso2}  {name[:34]:36} {status:32} {','.join(sorted(codes))}")

    print(f"\nORPHANED — seed says there is an archive, nobody scrapes it: {len(orphaned)}")
    for iso2, name in orphaned:
        print(f"  {iso2}  {name}")

    if undeclared:
        print(f"\nUNDECLARED — no COUNTRY_ISO2, cannot reconcile: {len(undeclared)}")
        print("  " + ", ".join(undeclared))
    if unknown_iso:
        print(f"\nISO2 NOT IN THE SEED: {len(unknown_iso)}")
        for iso2, codes in unknown_iso:
            print(f"  {iso2}  {','.join(codes)}")

    total = len(stale) + len(undeclared) + len(unknown_iso)
    if args.check and total:
        print(f"\n{total} mismatch(es). ORPHANED is a worklist, not a failure — "
              "the others mean the two inventories disagree.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
