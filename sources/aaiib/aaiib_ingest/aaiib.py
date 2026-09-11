# aaiib_ingest/aaiib.py
"""AAIIB (Philippines) HTML scraper: reports index + per-year listing discovery
and final-report PDF download.

Source: https://www.caap.gov.ph/reports/  (CAAP, hosting the Aircraft Accident
Investigation and Inquiry Board final reports).

Structure
  - /reports/ links to per-year listing pages /{YEAR}-accidents/ (2008..present).
  - Each year page is WordPress HTML; the accident final-report PDFs live under
    wp-content/uploads/... and are identified by a registration mark in the
    filename (RP-Cxxxx, RP-Rxxxx, foreign marks) together with an accident /
    final-report / interim-statement keyword.
  - Site-wide boilerplate PDFs (manuals, CSR statements, memos, etc.) appear on
    every year page and are excluded.
  - English, full ICAO Annex-13 reports with a text layer.

case_id
  AAIIB has no stable per-report id in URLs.  case_id is derived from the
  registration mark + year as  AAIIB-{YEAR}-{REG}  (e.g. AAIIB-2023-RP-C1174).
  The canonical "AAIIB-YYYY-NNN" board reference, when present in the PDF text,
  is stored separately as aaiib_ref.
"""
import html as _html
import re

BASE = "https://www.caap.gov.ph"
INDEX_URL = BASE + "/reports/"
REFERER = INDEX_URL
DELAY = 2.0

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

# Per-year listing pages: /{YEAR}-accidents[-N]/  (e.g. /2008-accidents-2/, /2024-accidents/)
_YEAR_LINK_RE = re.compile(
    r'href="(https?://[^"]*?/(\d{4})-accidents(?:-\d+)?/?)"',
    re.IGNORECASE,
)

# Any .pdf href
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)

# Filenames that ARE accident final reports: must contain a registration mark and
# an accident / final-report / interim keyword.
_ACCIDENT_KW_RE = re.compile(r"(accident|final-?report|interim)", re.IGNORECASE)

# Site-wide boilerplate PDFs to exclude (substring match on the URL).
_BOILERPLATE_RE = re.compile(
    r"(ownership|concession|corporate-governance|fit-and-proper|no-gift|"
    r"social-responsibility|\bcsr\b|aerodrome|qms|memo[_-]|environmental|"
    r"scorecard|procurement|accomplishment|safety-brief|directive|policy|"
    r"nprm|rule-?making|manual)",
    re.IGNORECASE,
)

# Registration mark in a filename.
#   RP-Cxxxx / RP-Rxxxx (Philippine) — most common
#   foreign: HL7525, A6-ENN, B-5498, etc.
_REG_RP_RE = re.compile(r"RP-[CR]\d{1,4}", re.IGNORECASE)
_REG_FOREIGN_RE = re.compile(
    r"\b(HL\d{3,4}|[A-Z]\d-[A-Z]{2,4}|[A-Z]{1,2}-\d{3,5})\b"
)

# Date suffix in some older filenames: _accident-MMDDYYYY  /  AccidentMMDDYYYY
_FN_DATE_RE = re.compile(r"(\d{2})(\d{2})(\d{4})\.pdf$", re.IGNORECASE)

# Canonical board reference inside the PDF text.
_AAIIB_REF_RE = re.compile(r"AAIIB-(\d{4})-(\d{2,4})", re.IGNORECASE)

# PDF header field extractors.
_PDF_DATE_RE = re.compile(
    r"DATE OF OCCURRENCE\s*:?\s*([A-Za-z]+\.?\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
_PDF_PLACE_RE = re.compile(
    r"PLACE OF OCCURRENCE\s*:?\s*(.+?)(?:\n\s*\n|AAIIB-|TABLE OF CONTENTS)",
    re.IGNORECASE | re.DOTALL,
)
_PDF_OPERATOR_RE = re.compile(r"OPERATOR\s*:?\s*(.+?)\n", re.IGNORECASE)


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(case_id: str) -> str:
    """Collapse whitespace and tidy separators around '-' / '/'.

    e.g. ' AAIIB - 2023 / RP-C1174 ' → 'AAIIB-2023/RP-C1174'.
    """
    s = (case_id or "").strip()
    s = re.sub(r"\s+", " ", s)
    # collapse whitespace immediately around - and /
    s = re.sub(r"\s*([-/])\s*", r"\1", s)
    return s.strip().upper()


def make_case_id(year, registration) -> str | None:
    """Build the AAIIB case_id from a year and a registration mark.

    Returns 'AAIIB-{YEAR}-{REG}' (normalized) or None when either part is
    missing.
    """
    if not year or not registration:
        return None
    return _normalize_case_id(f"AAIIB-{year}-{registration}")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_year_urls(index_html: str) -> list[str]:
    """Parse /reports/ index → ordered, de-duplicated list of per-year
    /{YEAR}-accidents/ listing URLs."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _YEAR_LINK_RE.finditer(index_html):
        url = _html.unescape(m.group(1))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _extract_registration(pdf_url: str) -> str | None:
    """Pull the registration mark out of a PDF filename."""
    fn = pdf_url.rsplit("/", 1)[-1]
    stem = fn[:-4] if fn.lower().endswith(".pdf") else fn
    m = _REG_RP_RE.search(stem)
    if m:
        return m.group(0).upper()
    m = _REG_FOREIGN_RE.search(stem)
    if m:
        return m.group(1).upper()
    return None


def _filename_date_iso(pdf_url: str) -> str | None:
    """Extract a YYYY-MM-DD date from the trailing _MMDDYYYY filename suffix."""
    fn = pdf_url.rsplit("/", 1)[-1]
    m = _FN_DATE_RE.search(fn)
    if not m:
        return None
    mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
    try:
        import datetime
        return datetime.date(int(yyyy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None


def is_accident_pdf(pdf_url: str) -> bool:
    """True when a PDF href is an AAIIB accident final-report (registration mark
    + accident keyword) and NOT site-wide boilerplate."""
    fn = pdf_url.rsplit("/", 1)[-1]
    if _BOILERPLATE_RE.search(pdf_url):
        return False
    if not _ACCIDENT_KW_RE.search(fn):
        return False
    if _extract_registration(pdf_url) is None:
        return False
    return True


def _pdf_priority(pdf_url: str) -> int:
    """Rank competing PDFs for the same registration in one year.

    Final reports win over plain accident reports, which win over interim
    statements.  Higher is better.  Several years list 2-3 PDFs per aircraft
    (e.g. two interim statements + the final report for HL7525/2022) — we keep
    only the most complete one.
    """
    fn = pdf_url.rsplit("/", 1)[-1].lower()
    if "interim" in fn:
        return 0
    if "final" in fn:
        return 3
    return 2  # plain "...accident..." report


def parse_listing(html: str, year: str = "") -> list[dict]:
    """Parse a per-year listing page → list of accident-report dicts.

    Each dict has:
      case_id            str   'AAIIB-{YEAR}-{REG}'
      pdf_url            str   absolute PDF URL
      registration       str
      date_of_occurrence str|None  (from filename suffix when present)
      event_class        str   'Accident'
      title              str   (PDF filename stem, human-ish)

    Rows whose filename yields no registration mark are skipped.  When several
    PDFs map to the same case_id (e.g. interim statements + final report), the
    highest-priority PDF (final report) is kept; ties keep the first seen.
    """
    best: dict[str, dict] = {}  # case_id -> row
    order: list[str] = []
    for m in _PDF_HREF_RE.finditer(html):
        pdf_url = _html.unescape(m.group(1))
        if not is_accident_pdf(pdf_url):
            continue
        reg = _extract_registration(pdf_url)
        if not reg:
            continue
        case_id = make_case_id(year, reg)
        if not case_id:
            continue

        fn = pdf_url.rsplit("/", 1)[-1]
        title = re.sub(r"[-_]+", " ", fn[:-4]).strip()
        row = {
            "case_id": case_id,
            "pdf_url": pdf_url,
            "registration": reg,
            "date_of_occurrence": _filename_date_iso(pdf_url),
            "event_class": "Accident",
            "title": title,
        }
        prev = best.get(case_id)
        if prev is None:
            best[case_id] = row
            order.append(case_id)
        elif _pdf_priority(pdf_url) > _pdf_priority(prev["pdf_url"]):
            best[case_id] = row

    return [best[cid] for cid in order]


# ──────────────────────────────────────────────
# PDF metadata extraction (post-download)
# ──────────────────────────────────────────────

# ── probable cause ────────────────────────────────────────────────────────────
#
# AAIIB reports carry a "PROBABLE CAUSE" heading on its own line, then either
# prose or a bulleted list, and end at the next all-caps heading. The heading
# vocabulary was measured across all 187 PDFs on the host rather than guessed:
#
#     SAFETY RECOMMENDATIONS   95
#     SAFETY RECOMMENDATION    24
#     SAFETY ACTIONS            1
#     CONTRIBUTORY FACTOR       1
#
# CONTRIBUTORY FACTOR ends the capture even though its content is arguably
# part of the cause: it is a separate section in the source, and folding it in
# would put text under a heading the report did not use.
#
# This exists because probable_cause is what decides whether a page is
# indexable at all. prod's quality score needs 50; a narrative over 300 chars
# scores 30 and a probable_cause over 100 scores 20, and the other three
# components (factors_json, weather_summary, phase_of_flight) are hardcoded
# null at projection. So those two fields together are the only route to 50,
# and this source had 169 rows with neither.
_PC_HEADING_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?PROBABLE\s+CAUSES?[ \t\f]*:?[ \t\f]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PC_TERMINATOR_RE = re.compile(
    r"^[ \t\f]*(?:\d+(?:\.\d+)*[.)]?[ \t\f]*)?"
    r"(?:SAFETY\s+RECOMMENDATIONS?|SAFETY\s+ACTIONS?|CONTRIBUTORY\s+FACTORS?"
    r"|CONCLUSIONS?|FINDINGS?|APPENDIC?E?S?|ANNEXE?S?)[ \t\f]*:?[ \t\f]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Page furniture that lands mid-section in the text layer.
_PC_FURNITURE_RE = re.compile(
    r"^[ \t\f]*(?:Page\s+\d+\s+of\s+\d+"
    r"|Investigation\s+Report\s+\S+"
    r"|Aircraft\s+Accident\s+Investigation\s+and\s+Inquiry\s+Board)[ \t\f]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Bullets arrive as whatever glyph the report's font mapped them to. The
# Wingdings bullet lands in the Private Use Area as U+F0B7, which an
# enumerated list of "known" bullet characters missed — so the whole PUA
# range is treated as a bullet rather than guessing which code points a
# future report will use.
_PC_BULLET_RE = re.compile(
    r"^[ \t\f]*[\u2022\u25aa\u25cf\u25a0\u00b7\u2013\u2014\-\*\uE000-\uF8FF]+[ \t\f]*",
    re.MULTILINE,
)

# Shorter than this is a heading echo or a stub, not a cause.
PROBABLE_CAUSE_MIN = 40


def parse_probable_cause(text: str) -> str | None:
    """Return the PROBABLE CAUSE section as one normalised string, or None.

    Bullets are flattened to sentences: prod renders this as a single field,
    and a list that arrives as "- a - b" reads worse than "a. b."
    """
    if not text:
        return None
    m = _PC_HEADING_RE.search(text)
    if not m:
        return None
    rest = text[m.end():]

    end = _PC_TERMINATOR_RE.search(rest)
    body = rest[: end.start()] if end else rest

    body = _PC_FURNITURE_RE.sub("", body)
    body = _PC_BULLET_RE.sub("", body)

    lines = [ln.strip() for ln in body.splitlines()]
    out = []
    for ln in lines:
        if not ln:
            continue
        if out and not out[-1].endswith((".", ";", ":")):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    joined = " ".join(out)
    joined = re.sub(r"\s+", " ", joined).strip()
    # A trailing fragment with no terminator usually means the capture ran into
    # the next page; keep it, but do not emit something too short to be a cause.
    return joined if len(joined) >= PROBABLE_CAUSE_MIN else None


def extract_pdf_metadata(text: str) -> dict:
    """Pull richer metadata out of the extracted PDF text.

    Returns a dict with keys: aaiib_ref, date_iso, location, operator,
    aircraft.  Any missing field is None.  All best-effort; the structured
    AAIIB header is consistent but field presence varies by report era.
    """
    out = {"aaiib_ref": None, "date_iso": None, "location": None,
           "operator": None, "aircraft": None}
    if not text:
        return out

    rm = _AAIIB_REF_RE.search(text)
    if rm:
        out["aaiib_ref"] = f"AAIIB-{rm.group(1)}-{rm.group(2)}"

    dm = _PDF_DATE_RE.search(text)
    if dm:
        from .text import month_name_date_to_iso
        out["date_iso"] = month_name_date_to_iso(dm.group(1))

    pm = _PDF_PLACE_RE.search(text)
    if pm:
        # trailing comma is layout noise (place lines wrap with a trailing ','),
        # but keep a trailing period (part of the value).
        place = re.sub(r"\s+", " ", pm.group(1)).strip().rstrip(",").strip()
        out["location"] = place or None

    om = _PDF_OPERATOR_RE.search(text)
    if om:
        op = re.sub(r"\s+", " ", om.group(1)).strip()
        out["operator"] = op or None

    return out


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest) -> None:
    """GET pdf_url with Referer header and write bytes to dest.

    Raises on non-2xx or empty body so per-row fetch isolation can keep the row
    'new' for retry (the web.caaplocal.ph mirror sometimes serves 0 bytes).
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    if not resp.content:
        raise RuntimeError("empty body")
    with open(dest, "wb") as fh:
        fh.write(resp.content)
