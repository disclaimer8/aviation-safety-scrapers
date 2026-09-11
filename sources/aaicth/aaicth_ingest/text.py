# aaicth_ingest/text.py
import re

_NONSLUG = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    if not s:
        return ""
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(case_id: str) -> str:
    """Make a URL-safe slug from case_id.

    'aaicth-7/2014' → 'aaicth-7-2014'
    """
    base = slugify(case_id)
    return base if base else "crash-aaicth"
