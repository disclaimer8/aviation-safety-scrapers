# aaiib_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "JANUARY 24, 2023" / "October 23, 2022" → ISO
_MONTH_DAY_YEAR_RE = re.compile(
    r"\b([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\b"
)


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
    """site_slug = lowercased case_id, [^a-z0-9]+ -> '-' (per source facts)."""
    base = slugify(case_id or "")
    return base if base else "aaiib"


def month_name_date_to_iso(s):
    """Parse 'JANUARY 24, 2023' (English month-name date) -> 'YYYY-MM-DD' or None."""
    s = (s or "").strip()
    m = _MONTH_DAY_YEAR_RE.search(s)
    if not m:
        return None
    month_name, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
    month = _MONTHS_EN.get(month_name)
    if not month:
        return None
    try:
        import datetime
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None
