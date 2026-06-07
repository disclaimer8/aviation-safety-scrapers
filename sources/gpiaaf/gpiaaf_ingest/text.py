# gpiaaf_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Portuguese slugs: transliterate the accents to ASCII so site_slug stays
# URL-clean (ã→a, ç→c, é→e, …).
_TRANSLIT = str.maketrans({
    "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a",
    "é": "e", "ê": "e", "è": "e",
    "í": "i", "î": "i",
    "ó": "o", "ô": "o", "õ": "o", "ò": "o",
    "ú": "u", "ü": "u",
    "ç": "c", "ñ": "n",
    "Á": "a", "À": "a", "Â": "a", "Ã": "a",
    "É": "e", "Ê": "e", "Í": "i", "Ó": "o", "Ô": "o",
    "Õ": "o", "Ú": "u", "Ç": "c",
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
    return f"crash-{base}" if base else "crash-gpiaaf"
