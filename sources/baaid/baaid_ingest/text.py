# baaid_ingest/text.py
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


def make_site_slug(case_id, aircraft=None, registration=None, location=None):
    """site_slug is the lowercased, slugified case_id (intrinsic Wix file-id or
    OCC number).  Aircraft/registration/location are accepted for API parity but
    the slug is keyed solely on case_id so it is stable and unique."""
    base = slugify(case_id)
    return base if base else "baaid"
