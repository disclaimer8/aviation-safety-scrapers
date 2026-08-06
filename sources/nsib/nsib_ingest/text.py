# nsib_ingest/text.py
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
    return f"crash-{base}" if base else "crash-nsib"


# Nigerian civil marks are exactly 5N- + 3 letters.  Filenames sometimes
# concatenate the operator onto the mark (5N-BRQOVERLAND, 5N-SHECAVERTON), so
# the Nigerian form is captured as a FIXED 3-letter mark (no trailing \b) to
# take just the registration.  Foreign marks seen: N-reg (USA), TC- (Turkey),
# EK- (Armenia), OD- (Lebanon) — these keep a trailing boundary.
_REG_NG_RE = re.compile(r"5N-?([A-Z]{3})")
# Leading (?<![A-Z0-9]) lets a mark sit after '_' or '-' (filename separators)
# while still rejecting it mid-token; trailing (?![A-Z0-9]) ends it cleanly.
_REG_FOREIGN_RE = re.compile(
    r"(?<![A-Z0-9])(N\d{2,5}[A-Z]{0,2}|TC-[A-Z]{3}|EK-\d{4,5}|OD-[A-Z]{3})(?![A-Z0-9])"
)


def clean_registration(raw):
    """
    Normalise a registration candidate to a canonical mark, or None.

    Accepts a free-text token (e.g. from a listing cell or a filename) and
    returns the first valid mark found, upper-cased.  Nigerian marks are taken
    as exactly 5N- + 3 letters even when followed by concatenated operator
    text (5N-BRQOVERLAND -> 5N-BRQ).  Returns None when no plausible mark exists
    (e.g. spilled cells like 'Limited', 'Helicopters', 'Wing').
    """
    if not raw:
        return None
    up = raw.upper()
    ng = _REG_NG_RE.search(up)
    fr = _REG_FOREIGN_RE.search(up)
    # prefer whichever mark appears first in the string
    cands = []
    if ng:
        cands.append((ng.start(), "5N-" + ng.group(1)))
    if fr:
        cands.append((fr.start(), fr.group(1)))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


# Structured case reference embedded in some interim PDF paths:
#   CODE/YYYY/MM/DD/INTR/NN   e.g. AAL/2024/12/11/INTR/02
_STRUCT_REF_RE = re.compile(
    r"\b([A-Z]{2,8})/(\d{4})/(\d{2})/(\d{2})/(INTR|PREL|FINL|ACC)/(\d{1,3})", re.I
)


def struct_ref_from_path(url_or_path):
    """
    Return a canonical case ref (e.g. 'AAL/2024/12/11/INTR/02') if the URL/path
    encodes one, else None.
    """
    if not url_or_path:
        return None
    m = _STRUCT_REF_RE.search(url_or_path)
    if not m:
        return None
    code, y, mo, d, kind, seq = m.groups()
    return f"{code.upper()}/{y}/{mo}/{d}/{kind.upper()}/{int(seq):02d}"


def make_case_id(struct_ref, registration, event_date, report_type=None):
    """
    Build an INTRINSIC case_id.

    Priority:
      1. structured report reference from the PDF path (e.g. AAL/2024/12/11/INTR/02)
      2. registration + event_date, namespaced by report_type to keep the
         preliminary and interim of the same airframe distinct:
            NSIB-<TYPE>-<REG>-<YYYY-MM-DD>
      3. (last resort) registration alone, or event_date alone.

    Returns a non-empty string, or None when nothing intrinsic is available.
    """
    if struct_ref:
        return struct_ref
    reg = clean_registration(registration)
    tcode = (report_type or "").strip().lower()
    if "interim" in tcode:
        tcode = "INT"
    elif "prelim" in tcode:
        tcode = "PRE"
    elif "final" in tcode:
        tcode = "FIN"
    else:
        tcode = "RPT"
    if reg and event_date:
        return f"NSIB-{tcode}-{reg}-{event_date}"
    if reg:
        return f"NSIB-{tcode}-{reg}"
    if event_date:
        return f"NSIB-{tcode}-{event_date}"
    return None
