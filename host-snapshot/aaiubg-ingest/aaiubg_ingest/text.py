# aaiubg_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
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


def make_site_slug(case_id):
    """Lowercased case_id with non-[a-z0-9] runs collapsed to '-'.

    e.g. 'LZ-PTS_2022-08-08' → 'lz-pts-2022-08-08'.
    """
    base = slugify(case_id)
    return base or "aaiubg"
