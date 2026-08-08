# aaibmy_ingest/text.py
#
# VENDORED from _common/text.py — do not edit here.
# Edit the canonical file and run `python -m _common.sync`; a test fails if a
# vendored copy drifts.
import html
import re

_TAG = re.compile(r"<[^>]+>")
# \s is Unicode-aware for str patterns, so this already collapses NBSP (U+00A0)
# and the other Zs separators that scraped markup is littered with — an
# explicit .replace("\xa0", " ") before it is redundant.
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")


def strip_html(s):
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def slugify(s):
    if not s:
        return ""
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-aaibmy"
