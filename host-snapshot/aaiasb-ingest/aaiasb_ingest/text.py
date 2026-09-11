# aaiasb_ingest/text.py
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
    # transliteration-free: keep ascii alnum only
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-aaiasb"


def normalize_case_id(s):
    """Normalize a report number like "01/2023" -> "01-2023", lowercase.
    Keeps e-prefix forms (e01-2022). Returns "" on empty."""
    s = (s or "").strip().lower()
    if not s:
        return ""
    s = s.replace("/", "-")
    s = re.sub(r"\s+", "", s)
    return s


def date_to_iso(s):
    """Convert DD/MM/YYYY to YYYY-MM-DD, or None on failure."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
