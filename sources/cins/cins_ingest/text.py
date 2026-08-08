# cins_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")

# Map Serbian-Latin diacritics to ASCII so slugs are clean a-z0-9.
_DIA = {
    "č": "c", "ć": "c", "đ": "dj", "š": "s", "ž": "z",
    "Č": "c", "Ć": "c", "Đ": "dj", "Š": "s", "Ž": "z",
}


def strip_html(s):
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def _deaccent(s):
    for k, v in _DIA.items():
        s = s.replace(k, v)
    return s


def slugify(s):
    if not s:
        return ""
    s = _deaccent(s.lower())
    return _NONSLUG.sub("-", s).strip("-")


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-cins"
