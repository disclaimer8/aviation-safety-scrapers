# gcaagy_ingest/gcaagy.py
"""GCAA Guyana (Aircraft Accident Investigation Unit) HTML scraper.

Source: https://www.gcaa-gy.org/AAID.html
- ONE static, server-rendered HTML page (English) listing every published
  final/accident report as <li><a href="pdf/...">TITLE</a></li>.
- ~29 PDFs spanning 2000-2022; the site is "stably stale".
- PDFs are text-layer English reports (21K-202K chars); one is scanned
  (source_tier "none"/"scanned").

case_id model
-------------
The listing exposes only a title + a stable PDF href.  The official file
reference ("AAIIU: N/N/YY/N") lives INSIDE the report text and is present in
only a minority of reports.  The PDF filename is therefore the one INTRINSIC,
stable identifier available for every row, so:

    case_id = make_case_id(href)            # 'gcaagy-<filename-slug>'

The AAIIU file reference, when found in the PDF text, is normalised by
_normalize_case_id() and stored in column report_url, which the build step
surfaces as report_type.  case_id itself stays intrinsic and stable; no
encounter-order suffixes are ever appended.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://www.gcaa-gy.org"
INDEX_URL = BASE + "/AAID.html"
REFERER = INDEX_URL
DELAY = 1.8

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Listing rows: <li> ... <a href="pdf/...pdf"> TITLE </a> ... </li>
_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref="(pdf/[^"]+?\.pdf)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Inner tags (icon <i>…</i>) stripped from link text
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NONSLUG_RE = re.compile(r"[^a-z0-9]+")

# Official file reference inside the PDF text:
#   "AAIIU: 3/1/22/3", "File No: AAIIU: 3.1.22", "AAIIU 3/1/33"
# Must NOT match the agency token "GAAIIU".  Separators are '/' or '.'.
_AAIIU_REF_RE = re.compile(
    r"(?<![A-Za-z])AAIIU\s*[:.]?\s*(\d+(?:\s*[/.]\s*\d+)+)",
)

# Guyanese (8R-XXX) or foreign (N-number, etc.) registration mark
_REG_RE = re.compile(r"\b(8R-?[A-Z]{2,4}|N\d{2,5}[A-Z]{0,2})\b")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# slug / case_id helpers
# ──────────────────────────────────────────────

def _slugify(s: str) -> str:
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def make_case_id(href: str) -> str:
    """
    Build the INTRINSIC, stable case_id from a PDF href.

    The filename stem is the only identifier present for every listing row, so
    it is the primary key.  e.g.
        'pdf/8R-GTR Final Report.pdf'  -> 'gcaagy-8r-gtr-final-report'
        'pdf/Fly_Jamaica_...pdf'       -> 'gcaagy-fly-jamaica-accident-final-report'

    No encounter-order suffix is ever appended; the same href always yields the
    same case_id.
    """
    href = _html.unescape(href or "")
    name = href.split("/")[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = urllib.parse.unquote(name)
    slug = _slugify(name)
    return f"gcaagy-{slug}" if slug else "gcaagy"


def _normalize_case_id(raw: str) -> str | None:
    """
    Normalise an official 'AAIIU' file reference to a canonical slug.

    Accepts the digit run captured by _AAIIU_REF_RE (separators '/' or '.',
    optional surrounding whitespace) and renders 'aaiiu-N-N-YY-N'.
        'AAIIU: 3/1/22/3'   -> 'aaiiu-3-1-22-3'
        'File No: AAIIU: 3.1.22' -> 'aaiiu-3-1-22'
        '3 / 1 / 33'        -> 'aaiiu-3-1-33'
    Returns None when no digit run is present.
    """
    if not raw:
        return None
    # raw may be the full match or just the digit group; isolate digit run
    m = _AAIIU_REF_RE.search(raw)
    digits = m.group(1) if m else raw
    parts = [p for p in re.split(r"[/.\s]+", digits.strip()) if p.isdigit()]
    if not parts:
        return None
    return "aaiiu-" + "-".join(parts)


def find_aaiiu_ref(text: str) -> str | None:
    """Return the normalised AAIIU file reference found in PDF text, or None."""
    if not text:
        return None
    m = _AAIIU_REF_RE.search(text)
    if not m:
        return None
    return _normalize_case_id(m.group(1))


def find_registration(text: str) -> str | None:
    """Return the first aircraft registration mark found in text, or None."""
    if not text:
        return None
    m = _REG_RE.search(text)
    if not m:
        return None
    reg = m.group(1).upper()
    # canonicalise 8RXXX -> 8R-XXX
    if reg.startswith("8R") and not reg.startswith("8R-"):
        reg = "8R-" + reg[2:]
    return reg


# ── cover-block field extraction ───────────────────────────────────
#
# Every report opens with a uniform labelled cover block, e.g.
#
#     Date of Accident       -   14 AUGUST 2021
#     Place of Accident / Region - HAAGS BOSCH SANITARY LANDFILL...
#     Aircraft Model / Type  - BN-2A-III-2 TRISLANDER
#     Registered Owner / Operator - RORAIMA AIRWAYS INC
#
# Labels vary slightly between reports ("Aircraft Model/Type" vs
# "Aircraft Manufacturer", "Place of Accident/Region", "Name of Operator",
# "Date of Occurrence"...) and the value can sit on the same line after the
# '-'/':' separator OR on the following line(s).  All extraction is best-effort
# and scoped to the cover block (text[:2500]); None when the field is absent.

_HEAD_CHARS = 2500

# English month names -> month number (case-insensitive).
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    # common abbreviations
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Word-month date with ordinal-suffix tolerance and optional comma before year:
#   "9TH NOVEMBER 2018", "21st February 2019", "18th January, 2014",
#   "14 AUGUST 2021", "1st June 2007".
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\.?\s+"      # day (+ optional ordinal)
    r"([A-Za-z]+)\.?\s*,?\s+"                    # month word (+ optional comma)
    r"(\d{4})\b",                               # year
    re.IGNORECASE,
)

# "Date of Accident", "Date of Occurrence" (slash/space tolerant) label.
_DATE_LABEL_RE = re.compile(
    r"Date\s+of\s+(?:Accident|Occurrence|Incident|Event)",
    re.IGNORECASE,
)


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _word_date_at(text: str) -> str | None:
    """Return the first word-month date in text as ISO, or None."""
    for m in _WORD_DATE_RE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if month is None:
            continue
        iso = _iso(int(m.group(1)), month, int(m.group(3)))
        if iso:
            return iso
    return None


def extract_event_date(text: str | None) -> str | None:
    """
    Extract the ISO 'YYYY-MM-DD' event date from the report cover block.

    Strategy: scan only the cover/title block (text[:2500]).  Prefer the first
    word-month date that follows a "Date of Accident"/"Date of Occurrence"
    label (most robust); otherwise fall back to the first word-month date in
    the head.  Tolerates ordinal suffixes (1st/2nd/3rd/14th/9TH), an optional
    comma before the year and case-insensitive month names.

    Returns None when no parseable date is present (older scanned reports).
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]

    label = _DATE_LABEL_RE.search(head)
    if label:
        # Look just after the label (same + a few following lines) first.
        window = head[label.end():label.end() + 120]
        iso = _word_date_at(window)
        if iso:
            return iso

    return _word_date_at(head)


# Possessive throughout. The original was
#     \s*[-:–—]?\s*(?:\n\s*)*([^\n]+)
# and \s already matches \n, so \s* and (?:\n\s*)* could divide the same
# run of newlines exponentially many ways. On input that never reaches
# [^\n]+ — a scanned PDF whose text layer is blank lines — match time
# doubled per newline: 24 newlines took 0.9s. Possessive quantifiers give
# nothing back, so there is no backtracking to blow up. Python 3.11+, which
# this tree already requires.
_VALUE_RE = re.compile(
    r"[^\S\n]*+(?:\n[^\S\n]*+)*+[-:–—]?[^\S\n]*+(?:\n[^\S\n]*+)*+([^\n]+)"
)


def _extract_labelled(text: str, label_re: re.Pattern) -> str | None:
    """
    Return the value following a cover-block label.

    The value may sit on the same line after a '-'/':' separator, or on the
    following non-blank line.  Captures a single line, trimmed of separators
    and surrounding whitespace.  None when the label is absent or empty.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = label_re.search(head)
    if not m:
        return None
    rest = head[m.end():]
    # consume the separator and any blank lines, then take the first line of value
    vm = _VALUE_RE.match(rest)
    if not vm:
        return None
    val = vm.group(1)
    val = re.sub(r"^[\s\-:–—]+", "", val)
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


_AIRCRAFT_LABEL_RE = re.compile(
    r"Aircraft\s+(?:Model|Make)\s*/?\s*(?:Type)?",
    re.IGNORECASE,
)
_LOCATION_LABEL_RE = re.compile(
    r"Place\s+of\s+(?:Accident|Occurrence|Incident)\s*/?\s*(?:Region)?",
    re.IGNORECASE,
)
_OPERATOR_LABEL_RE = re.compile(
    r"(?:Registered\s+Owner\s*/?\s*Operator|Name\s+of\s+Operator|Operator)",
    re.IGNORECASE,
)


def extract_aircraft(text: str | None) -> str | None:
    """Aircraft make/model from the cover block ('Aircraft Model/Type - ...')."""
    return _extract_labelled(text or "", _AIRCRAFT_LABEL_RE)


def extract_location(text: str | None) -> str | None:
    """Event location from the cover block ('Place of Accident/Region - ...')."""
    return _extract_labelled(text or "", _LOCATION_LABEL_RE)


def extract_operator(text: str | None) -> str | None:
    """Operator from the cover block ('Registered Owner/Operator - ...')."""
    return _extract_labelled(text or "", _OPERATOR_LABEL_RE)


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def parse_listing(index_html: str) -> list[dict]:
    """
    Parse the GCAA AAID index page → list of report dicts.

    Each dict has:
      case_id   str   intrinsic 'gcaagy-<slug>' from the PDF filename
      pdf_url   str   absolute URL (href is left verbatim; the downloader
                      URL-encodes any spaces at request time)
      title     str   cleaned link text (icon tags stripped)

    Order preserved; de-duplicated by case_id.
    """
    seen: set[str] = set()
    rows: list[dict] = []
    for m in _PDF_LINK_RE.finditer(index_html):
        href = _html.unescape(m.group(1).strip())
        title = _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub("", m.group(2)))).strip()
        case_id = make_case_id(href)
        if case_id in seen:
            continue
        seen.add(case_id)
        pdf_url = f"{BASE}/{href}"
        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "title": title,
        })
    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with Referer header and write bytes to dest.

    The path component is percent-encoded so hrefs containing spaces (e.g.
    '8R-GTR Final Report.pdf') resolve correctly while the listing href is
    stored verbatim.  Raises httpx.HTTPStatusError on non-2xx responses.
    """
    parts = urllib.parse.urlsplit(pdf_url)
    safe_path = urllib.parse.quote(parts.path, safe="/%")
    encoded = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )
    resp = client.get(encoded, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
