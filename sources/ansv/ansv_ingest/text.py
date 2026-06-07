# ansv_ingest/text.py
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
    return f"crash-{base}" if base else "crash-ansv"


def fr_date_to_iso(s):
    """Convert DD/MM/YYYY (4-digit year only) to YYYY-MM-DD, or None on failure."""
    s = (s or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


# ── piecewise title parser ────────────────────────────────────────────────────
# Event titles follow the pattern:
#   <EventClass> to the <Aircraft> (registered|identified) <Reg>
#   [operated by <Operator>] on <DD/MM/YY[YY]> (at|near|off|…) <Location>
#   [<trailing bracket>]
#
# Piecewise approach extracts each field independently so one mismatch
# does not cascade into a total parse failure.

_CLS_RE = re.compile(
    r"^(Accident|Serious incident|Incident)\b", re.IGNORECASE
)

# Accept DD/MM/YY (2-digit) or DD/MM/YYYY (4-digit)
_DATE_TOKEN_RE = re.compile(
    r"\bon\s+(\d{1,2}/\d{1,2}/\d{2,4})\b", re.IGNORECASE
)

# First "registered" or "identified" keyword followed by a registration mark
_REG_RE = re.compile(
    r"\b(?:registered|identified)\s+([A-Z0-9][A-Z0-9-]*)\b", re.IGNORECASE
)

# Aircraft type: text between "to the " and first " registered " / " identified "
_AC_TYPE_RE = re.compile(
    r"\bto the\s+(.+?)\s+(?:registered|identified)\b", re.IGNORECASE
)

# Operator: "operated by X" where X ends before " on ", " and ", a comma, or EOS
_OP_RE = re.compile(
    r"\boperated by\s+(.+?)(?=\s+on\s+\d|\s+and\s+|\s+registered\b|\s+identified\b|,|$)",
    re.IGNORECASE,
)

# Location-prefix prepositions (longest first to avoid partial matches)
_LOC_PREP_RE = re.compile(
    r"^(?:en route to|close to|on the|en route|near|off|at|on)\s*",
    re.IGNORECASE,
)

# Trailing "[Investigation led by ...]" or "[...]" bracket
_BRACKET_RE = re.compile(r"\s*\[.*?\]\s*$", re.IGNORECASE | re.DOTALL)

# Current year ceiling for 2-digit year normalisation
_YY_CEIL = 2027  # 2-digit YY → 2000+YY when <= _YY_CEIL, else 1900+YY


def _normalise_date(raw):
    """Convert D/M/YY or D/M/YYYY string to ISO YYYY-MM-DD, or None."""
    parts = raw.split("/")
    if len(parts) != 3:
        return None
    try:
        d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if len(parts[2]) == 2:
        # 2-digit year: 2000+YY if plausible, else 1900+YY
        y = 2000 + y if (2000 + y) <= _YY_CEIL else 1900 + y
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_event_title(title):
    """
    Piecewise parser for event titles.

    Returns a dict with keys:
        event_class, aircraft_type, registration, date_iso, location, operator
    All values are str or None.  Returns all-None when the title contains none
    of event_class, date and registration (i.e. clearly not a real event title).
    """
    out = {"event_class": None, "aircraft_type": None, "registration": None,
           "date_iso": None, "location": None, "operator": None}

    t = (title or "").strip()
    if not t:
        return out

    # 1. event_class
    cm = _CLS_RE.match(t)
    event_class = None
    if cm:
        raw_cls = cm.group(1).lower()
        if raw_cls == "accident":
            event_class = "Accident"
        elif raw_cls == "serious incident":
            event_class = "Serious incident"
        else:
            event_class = "Incident"

    # 2. date  (acts as the location anchor)
    dm = _DATE_TOKEN_RE.search(t)
    date_iso = None
    date_end = None  # index in t just after the full date token (incl. trailing \b)
    if dm:
        date_iso = _normalise_date(dm.group(1))
        date_end = dm.end()

    # 3. registration  (first occurrence — handles multi-aircraft titles)
    rm = _REG_RE.search(t)
    registration = rm.group(1).upper() if rm else None

    # 4. aircraft type: between "to the " and the first reg/identified keyword
    acm = _AC_TYPE_RE.search(t)
    aircraft_type = acm.group(1).strip() if acm else None

    # 5. operator
    opm = _OP_RE.search(t)
    operator = opm.group(1).strip() if opm else None

    # 6. location: everything after the date token, stripped of leading preposition
    #    and trailing bracket / investigation note.
    location = None
    if date_end is not None:
        loc_raw = t[date_end:].strip()
        # strip trailing "[Investigation led by ...]" / any bracket
        loc_raw = _BRACKET_RE.sub("", loc_raw).strip()
        # strip trailing "investigation led by ..." (un-bracketed variant)
        loc_raw = re.sub(
            r"\s+investigation led by\s+.*$", "", loc_raw, flags=re.IGNORECASE
        ).strip()
        # strip leading comma/semicolon separator
        loc_raw = re.sub(r"^[,;]\s*", "", loc_raw).strip()
        # strip leading location preposition
        loc_stripped = _LOC_PREP_RE.sub("", loc_raw).strip()
        # If stripping left nothing but the original was "en route…", keep "en route"
        if not loc_stripped and loc_raw.lower().startswith("en route"):
            location = "en route"
        else:
            location = loc_stripped

    # Reject clearly non-event titles (no class, no date, no registration)
    if event_class is None and date_iso is None and registration is None:
        return out

    out.update(
        event_class=event_class,
        aircraft_type=aircraft_type,
        registration=registration,
        date_iso=date_iso,
        location=location,
        operator=operator,
    )
    return out
