# ainhr_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")

# Croatian diacritic folding for slug generation.
_DIACRITICS = {
    "č": "c", "ć": "c", "ž": "z", "š": "s", "đ": "d",
    "Č": "c", "Ć": "c", "Ž": "z", "Š": "s", "Đ": "d",
}


def fold_diacritics(s):
    if not s:
        return ""
    return "".join(_DIACRITICS.get(ch, ch) for ch in s)


def strip_html(s):
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def slugify(s):
    if not s:
        return ""
    s = fold_diacritics(s)
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(case_id):
    """site_slug = lowercased case_id with [^a-z0-9]+ -> '-'.

    The AIN.HR post slug is intrinsic (aircraft-location-date) and already in
    that shape, so this is effectively a normalising pass-through.
    """
    base = slugify(case_id)
    return base or "ainhr"
