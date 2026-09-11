# aaiahk_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")

# Civil aircraft registrations in AAIA Hong Kong PDF bodies. Pattern handles
# four observed layouts:
#   1. "Aircraft, B-KPQ,"                — HK style, comma-separated
#   2. "Aircraft\nN406KZ\nLocation"      — newline-separated
#   3. "Aircraft B-LUK\nLocation"        — space-separated
#   4. "registration mark N406KZ"        — descriptive prose
#
# Strategy: look for a country-prefixed reg PATTERN with word-style boundary
# guards (no preceding/trailing alnum-or-hyphen → won't fire inside a longer
# token like "Cap. 448B" or model numbers like "Boeing 737-800"); restrict
# the search window to the FIRST 2500 characters of the PDF text (the header
# region where format is consistent); return the first hit. Patterns are
# tightened to minimum sensible lengths so a stray "N2" cannot match.
_REG_PATTERN = re.compile(
    r"""
    (?<![A-Z0-9-])
    (
        B-[A-Z]{2,4}                # Hong Kong civil
      | N\d{2,5}[A-Z]{0,2}          # USA (min 2 digits → no N2)
      | G-[A-Z]{4}                  # United Kingdom
      | VH-[A-Z]{3}                 # Australia
      | 9V-[A-Z]{3}                 # Singapore
      | D-[A-Z]{4}                  # Germany
      | F-[A-Z]{4}                  # France
      | VT-[A-Z]{3}                 # India
      | JA\d{3,4}[A-Z]?             # Japan
      | EI-[A-Z]{3}                 # Ireland
      | HS-[A-Z]{3}                 # Thailand
      | A6-[A-Z]{3}                 # UAE
      | OE-[A-Z]{3}                 # Austria
      | OK-[A-Z]{3}                 # Czechia
      | PH-[A-Z]{3}                 # Netherlands
      | C-[A-Z]{4}                  # Canada
      | RP-C\d{3,5}                 # Philippines
      | RA-\d{4,5}                  # Russia
      | TC-[A-Z]{3}                 # Turkey
      | UR-[A-Z0-9]{4,5}            # Ukraine
      | OY-[A-Z]{3}                 # Denmark
      | SE-[A-Z]{3}                 # Sweden
      | LN-[A-Z]{3}                 # Norway
      | EC-[A-Z]{3}                 # Spain
      | I-[A-Z]{4}                  # Italy
    )
    (?![A-Z0-9-])
    """,
    re.IGNORECASE | re.VERBOSE,
)
_REG_HEADER_WINDOW = 2500


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
    return f"crash-{base}" if base else "crash-aaiahk"


def extract_registration(narrative_text):
    """Best-effort: pull a civil aviation registration out of an AAIA Hong
    Kong report's PDF body. Returns the canonical uppercase form or None.

    Used in pipeline.parse() to back-fill aaiahk_reports.registration after
    the PDF is downloaded, so prelim/interim/final tier upgrades for the
    same physical event dedup cleanly via (event_date + registration)
    instead of falling through to per-source occurrence keys.

    Search window is the first 2500 characters of the PDF text (consistent
    header region: "Aircraft type, REG," or "Aircraft\\nREG\\nLocation").
    Paragliders / hang gliders / paramotors have no civil registration and
    return None.
    """
    if not narrative_text:
        return None
    head = narrative_text[:_REG_HEADER_WINDOW]
    m = _REG_PATTERN.search(head)
    return m.group(1).upper() if m else None
