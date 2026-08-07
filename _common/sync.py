# _common/sync.py — vendor the canonical modules into the source packages.
#
#     python -m _common.sync           # write the vendored copies
#     python -m _common.sync --check   # report drift, exit 1 (what CI runs)
#
# Why vendoring rather than a shared package: every source is deployed as its
# own directory with its own virtualenv, and the README promises a source can
# be copied away and run on its own. A real import dependency would mean
# installing one more package into every venv and would break that promise for
# a benefit we can get with a generator and a test.
#
# VENDORED is an explicit opt-in list rather than a glob over sources/. Not
# every package's pdf.py is the same lineage — a few carry per-source tweaks —
# and syncing the canon over one of those would silently delete real logic.
# A package joins this list only after its variant has been read and confirmed
# to be the canonical one. (nsib is why this matters: its text.py is the
# canonical three functions plus Nigerian registration parsing, so the canon
# lands beside them as _textbase.py and text.py re-exports it.)
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANON_DIR = ROOT / "_common"
SOURCES_DIR = ROOT / "sources"

# source -> {canonical module: module name to write inside the package}
VENDORED = {
    "aaibmy": {"pdf": "pdf", "http": "httpc", "text": "text"},
    "ahac": {"pdf": "pdf", "http": "httpc", "text": "text"},
    "ciaiauy": {"pdf": "pdf", "http": "httpc", "text": "text"},
    "nsib": {"pdf": "pdf", "http": "httpc", "text": "_textbase"},
    "ntsbaar": {"pdf": "pdf", "http": "httpc", "text": "text"},
    "ovv": {"pdf": "pdf", "http": "httpc", "text": "text"},
    # rosap talks to the site through a browser, not httpx, so it takes
    # pdf (for OCR) and text but has no use for the http client.
    "rosap": {"pdf": "pdf", "text": "text"},
    "sacaa": {"pdf": "pdf", "http": "httpc", "text": "text"},
}

_TOKEN = re.compile(r"\{\{([A-Z_]+)\}\}")


def read_canon(module):
    return (CANON_DIR / f"{module}.py").read_text(encoding="utf-8")


def vendor_path(source, module):
    return SOURCES_DIR / source / f"{source}_ingest" / f"{module}.py"


def render(canon, source, module):
    """Substitute the template tokens; an unknown token is an error."""
    values = {"SOURCE": source, "MODULE": module}

    def sub(match):
        name = match.group(1)
        if name not in values:
            raise ValueError(
                f"unknown token {{{{{name}}}}} in canon for {source}/{module}"
            )
        return values[name]

    return _TOKEN.sub(sub, canon)


def expected_text(source, canon_module, target_module):
    return render(read_canon(canon_module), source, target_module)


def _pairs():
    for source, modules in sorted(VENDORED.items()):
        for canon_module, target_module in sorted(modules.items()):
            yield source, canon_module, target_module


def find_drift():
    """Return 'source/module' for every vendored copy that is stale."""
    drifted = []
    for source, canon_module, target_module in _pairs():
        target = vendor_path(source, target_module)
        expected = expected_text(source, canon_module, target_module)
        actual = target.read_text(encoding="utf-8") if target.exists() else None
        if actual != expected:
            drifted.append(f"{source}/{target_module}")
    return drifted


def write_all():
    """Write every vendored copy; return the ones that changed."""
    changed = []
    for source, canon_module, target_module in _pairs():
        target = vendor_path(source, target_module)
        expected = expected_text(source, canon_module, target_module)
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding="utf-8")
            changed.append(f"{source}/{target_module}")
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="_common.sync")
    ap.add_argument("--check", action="store_true",
                    help="report drift and exit 1 instead of writing")
    args = ap.parse_args(argv)

    if args.check:
        drifted = find_drift()
        for name in drifted:
            print(f"drifted: {name}")
        if drifted:
            print(f"\n{len(drifted)} vendored file(s) stale — run: python -m _common.sync")
            return 1
        print(f"in sync: {sum(1 for _ in _pairs())} vendored files")
        return 0

    changed = write_all()
    for name in changed:
        print(f"wrote: {name}")
    print(f"{len(changed)} changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
