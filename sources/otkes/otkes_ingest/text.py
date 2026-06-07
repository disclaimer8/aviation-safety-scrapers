# otkes_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Finnish slugs: transliterate the umlauts/diacritics to ASCII so site_slug
# stays URL-clean (ä→a, ö→o, å→a).
_TRANSLIT = str.maketrans({
    "ä": "a", "ö": "o", "å": "a",
    "Ä": "a", "Ö": "o", "Å": "a",
    "é": "e", "ü": "u",
})
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
    s = s.lower().translate(_TRANSLIT)
    return _NONSLUG.sub("-", s).strip("-")


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-otkes"
