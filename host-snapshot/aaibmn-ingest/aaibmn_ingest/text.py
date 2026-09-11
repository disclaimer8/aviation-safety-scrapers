# aaibmn_ingest/text.py
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


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-aaibmn"


# ── date parsing ───────────────────────────────────────────────────────────────
# Mongolia AAIB titles carry the event date in several formats, e.g.
#   (2024.02.27)...            -> YYYY.MM.DD prefix
#   ...Japan, 2016.09.09        -> YYYY.MM.DD suffix
#   04 Aug.2016                 -> DD Mon.YYYY
#   18.Oct.2013 / 18 Oct. 2013  -> DD.Mon.YYYY
#   20 May. 2013                -> DD Mon. YYYY
#   02.06.2017                  -> DD.MM.YYYY
#   03 May.2016                 -> DD Mon.YYYY

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# YYYY.MM.DD (or YYYY-MM-DD / YYYY/MM/DD)
_DATE_YMD_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
# DD <Mon>[.] [ ]YYYY  e.g. "04 Aug.2016", "18.Oct.2013", "20 May. 2013", "03 May.2016"
_DATE_DMONY_RE = re.compile(
    r"\b(\d{1,2})[.\s]+([A-Za-z]{3,9})\.?\s*(\d{4})\b"
)
# DD.MM.YYYY (numeric, day first)  e.g. "02.06.2017"
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")


def _try_date(year, month, day):
    try:
        import datetime
        return datetime.date(int(year), int(month), int(day)).isoformat()
    except (ValueError, TypeError):
        return None


def parse_event_date(title):
    """Extract an ISO YYYY-MM-DD event date from a Mongolia AAIB title.

    Tries YYYY.MM.DD first (most common), then 'DD Mon YYYY', then DD.MM.YYYY.
    Returns ISO string or None.
    """
    if not title:
        return None
    t = title.strip()

    m = _DATE_YMD_RE.search(t)
    if m:
        iso = _try_date(m.group(1), m.group(2), m.group(3))
        if iso:
            return iso

    m = _DATE_DMONY_RE.search(t)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            iso = _try_date(m.group(3), mon, m.group(1))
            if iso:
                return iso

    m = _DATE_DMY_RE.search(t)
    if m:
        iso = _try_date(m.group(3), m.group(2), m.group(1))
        if iso:
            return iso

    return None


# ── registration parsing ───────────────────────────────────────────────────────
# Mongolian marks: JU-NNNN or JUNNNN (no dash). Foreign marks: EI-CXV, RA-2099G.
# Prefer a JU mark when present, else the first plausible foreign mark.
_REG_JU_RE = re.compile(r"\bJU-?\d{3,4}[A-Z]?\b", re.IGNORECASE)
_REG_FOREIGN_RE = re.compile(r"\b([A-Z]{1,2}-[A-Z0-9]{2,5})\b")


def parse_registration(title):
    if not title:
        return None
    m = _REG_JU_RE.search(title)
    if m:
        reg = m.group(0).upper()
        # normalise JUNNNN -> JU-NNNN
        if not reg.startswith("JU-"):
            reg = "JU-" + reg[2:]
        return reg
    m = _REG_FOREIGN_RE.search(title)
    if m:
        return m.group(1).upper()
    return None


# ── event class parsing ─────────────────────────────────────────────────────────
def parse_event_class(title):
    if not title:
        return None
    low = title.lower()
    if "serious incident" in low:
        return "Serious incident"
    if "accident" in low:
        return "Accident"
    if "incident" in low:
        return "Incident"
    return None
