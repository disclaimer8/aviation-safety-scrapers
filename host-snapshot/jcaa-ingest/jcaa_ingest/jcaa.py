# jcaa_ingest/jcaa.py
"""Jamaica Civil Aviation Authority (JCAA) scraper.

Source: https://jcaa.gov.jm/accident-investigations/
        WordPress + Sucuri CDN; Elementor-rendered page.

The accident-investigations page uses Elementor Pro loop widgets to render
investigation posts.  The final-report PDFs are NOT linked in the rendered
HTML — they are served from wp-content/uploads and discoverable via the
WP REST API (wp-json/wp/v2/media?search=…&mime_type=application/pdf).

Discovery strategy
------------------
1. Fetch the accident-investigations page HTML and parse it for any PDF
   anchors (handles future changes if buttons become plain <a> tags).
2. Query the WP REST API media endpoint with several search terms (final
   report, investigation report) to catch all uploaded accident PDFs.
3. De-duplicate by URL; skip non-accident PDFs (statistical reports,
   press releases, SNOWTAMs, etc.) using a conservative filename filter.

Known reports (as of 2025):
  1996-May-30-6Y-JGT-Final-Report-1.pdf           1996-05-30  6Y-JGT
  2008-Jul-07-AJM036-Final-Report-SERIOUS-INCIDENT.pdf  2008-07-07  (serious incident, skip)
  2009-Dec-22-AA331-FINAL-REPORT.pdf               2009-12-22  N977AN
  2014-Mar-31-JBU876-Final-Report.pdf              2014-03-31  N-number
  2014-Sept-05-N900KN-Final-Report.pdf             2014-09-05  N900KN
  2016-Nov-10-N101KA-Final-Report.pdf              2016-11-10  N101KA

The 2008 serious-incident report is EXCLUDED (it is an incident, not an
accident; the filename contains "SERIOUS-INCIDENT").

The 2024 N51157 and 2023 N3254B investigations are in-progress with no
published final PDF — they are explicitly excluded by filename pattern.

case_id model
-------------
The PDF filename is the intrinsic, stable identifier available for every row:

    JCAA-<YEAR>-<REG>    normalised from date+reg in the filename, e.g.
                          JCAA-1996-6Y-JGT, JCAA-2009-N977AN

When date and registration cannot be parsed from the filename, we fall back
to a slugified filename stem:  JCAA-<slug>.

country: JM  (Jamaican operations, though AA331 was a US-registered aircraft)
Registration: Jamaican regs use 6Y-XXX; foreign regs (N977AN etc.) also appear.
"""
import re
import time
from pathlib import Path

BASE = "https://www.jcaa.gov.jm"
INDEX_URL = BASE + "/accident-investigations/"
WP_MEDIA_API = BASE + "/wp-json/wp/v2/media"
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE + "/",
}

# ── WP media search terms that catch accident final reports ─────────────────
_MEDIA_SEARCH_TERMS = [
    "final report",
    "investigation report",
    "accident investigation",
]

# ── Filename filters ────────────────────────────────────────────────────────
# Filenames that must NOT be ingested (press releases, stats, incidents).
_SKIP_FILENAME_RE = re.compile(
    r"SERIOUS.?INCIDENT"
    r"|PRESS.RELEASE"
    r"|NEWS.RELEASE"
    r"|STATISTICAL"
    r"|ANNUAL"
    r"|SNOWTAM"
    r"|ADVISORY"
    r"|SAFETY.REPORT"
    r"|SUSPECTED"
    r"|AIRSPACE",
    re.IGNORECASE,
)

# Filenames that MUST contain at least one of these to be a final/investigation report.
_KEEP_FILENAME_RE = re.compile(
    r"FINAL.REPORT"
    r"|INVESTIGATION.REPORT"
    r"|ACCIDENT.REPORT",
    re.IGNORECASE,
)

# ── Date + registration parsing from filename ────────────────────────────────
# e.g. "1996-May-30-6Y-JGT-Final-Report-1"
#      "2009-Dec-22-AA331-FINAL-REPORT"
#      "2014-Sept-05-N900KN-Final-Report"
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_IN_FILENAME_RE = re.compile(
    r"^(\d{4})-([A-Za-z]{3,4})-(\d{1,2})-(.+?)[-._](?:Final|Accident|Investigation)",
    re.IGNORECASE,
)
# Registration patterns in filename (after the date part)
# 6Y-JGT, N977AN, N900KN, JBU876 (N-number style), N101KA, AA331 (airline+flight)
_REG_IN_FILENAME_RE = re.compile(
    r"(6Y-?[A-Z0-9]{2,5}|N\d{2,5}[A-Z]{0,2}|[A-Z]{2,3}\d{2,4})",
    re.IGNORECASE,
)

_NONSLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s):
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def make_case_id(filename_stem):
    """Build an intrinsic case_id from a PDF filename stem.

    Strategy: parse YYYY-Mon-DD-<REG> from the stem, normalise to
    JCAA-<YEAR>-<REG>.  Fall back to JCAA-<slugified stem> when the date
    pattern is absent.

    e.g.
      '1996-May-30-6Y-JGT-Final-Report-1'  -> 'JCAA-1996-6Y-JGT'
      '2009-Dec-22-AA331-FINAL-REPORT'     -> 'JCAA-2009-AA331'
      '2014-Sept-05-N900KN-Final-Report'   -> 'JCAA-2014-N900KN'
      '2014-Mar-31-JBU876-Final-Report'    -> 'JCAA-2014-JBU876'
    """
    m = _DATE_IN_FILENAME_RE.match(filename_stem)
    if m:
        year = m.group(1)
        # group(4) is the registration/flight number segment
        reg_part = m.group(4).strip("-")
        # normalise: uppercase, collapse non-alnum to hyphens
        reg_norm = re.sub(r"[^A-Z0-9]+", "-", reg_part.upper()).strip("-")
        # For the first segment only (before next hyphen group)
        # e.g. 'AA331' not 'AA331-N977AN'
        return f"JCAA-{year}-{reg_norm}"
    # fallback: slugified stem
    slug = _slugify(filename_stem)
    return f"JCAA-{slug}" if slug else "JCAA-unknown"


def _is_accident_pdf(source_url, title=""):
    """Return True when a media URL looks like an accident investigation report."""
    fname = source_url.split("/")[-1]
    if _SKIP_FILENAME_RE.search(fname):
        return False
    if not fname.lower().endswith(".pdf"):
        return False
    if _KEEP_FILENAME_RE.search(fname):
        return True
    # Also accept if title contains the keep words
    if title and _KEEP_FILENAME_RE.search(title):
        return True
    return False


def _extract_from_filename(source_url):
    """Extract year, registration, event_date from PDF URL/filename."""
    fname = source_url.split("/")[-1]
    stem = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)

    m = _DATE_IN_FILENAME_RE.match(stem)
    year = None
    event_date = None
    registration = None

    if m:
        year_str = m.group(1)
        month_str = m.group(2).lower()
        day_str = m.group(3)
        reg_segment = m.group(4).strip("-")
        year = int(year_str)
        month_num = _MONTHS.get(month_str[:3])
        if month_num:
            try:
                event_date = f"{year:04d}-{month_num:02d}-{int(day_str):02d}"
            except ValueError:
                pass
        # Registration: first recognisable mark from the reg_segment
        reg_m = _REG_IN_FILENAME_RE.search(reg_segment)
        if reg_m:
            reg = reg_m.group(1).upper()
            # canonicalise 6YJGT -> 6Y-JGT
            reg = re.sub(r"^6Y([A-Z0-9]+)$", r"6Y-\1", reg)
            registration = reg

    return year, event_date, registration, stem


# ── Cover-block field extraction from report text ───────────────────────────

_HEAD_CHARS = 3000

_DATE_LABEL_RE = re.compile(
    r"Date\s+(?:and\s+Time\s+)?of\s+(?:Accident|Occurrence|Incident|Event)",
    re.IGNORECASE,
)
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\.?\s+"
    r"([A-Za-z]+)\.?\s*,?\s+"
    r"(\d{4})\b",
    re.IGNORECASE,
)
# "22 December 2009", "22nd December 2009"
_WORD_DATE2_RE = re.compile(
    r"\b([A-Za-z]+)\s+(\d{1,2})\s*(?:st|nd|rd|th)?\.?\s*,?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _iso(day, month, year):
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _try_word_date(text):
    """Try day-month-year and month-day-year word patterns."""
    for m in _WORD_DATE_RE.finditer(text):
        month = _MONTHS_EN.get(m.group(2).lower())
        if month:
            iso = _iso(int(m.group(1)), month, int(m.group(3)))
            if iso:
                return iso
    for m in _WORD_DATE2_RE.finditer(text):
        month = _MONTHS_EN.get(m.group(1).lower())
        if month:
            iso = _iso(int(m.group(2)), month, int(m.group(3)))
            if iso:
                return iso
    return None


def extract_event_date(text):
    """Extract ISO event date from report text cover block."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    lbl = _DATE_LABEL_RE.search(head)
    if lbl:
        window = head[lbl.end():lbl.end() + 150]
        iso = _try_word_date(window)
        if iso:
            return iso
    return _try_word_date(head)


_AIRCRAFT_LABEL_RE = re.compile(
    r"Aircraft\s+(?:Make\s*/\s*Model|Model\s*/?\s*Type|Type)",
    re.IGNORECASE,
)
_LOCATION_LABEL_RE = re.compile(
    r"(?:Location|Place)\s+of\s+(?:Accident|Occurrence|Incident)",
    re.IGNORECASE,
)
_OPERATOR_LABEL_RE = re.compile(
    r"(?:Registered\s+(?:Owner\s*/\s*)?)?Operator",
    re.IGNORECASE,
)
_REG_LABEL_RE = re.compile(
    r"(?:Registration|Aircraft\s+Registration)\s*(?:Number)?",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")


def _labelled(text, label_re):
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = label_re.search(head)
    if not m:
        return None
    rest = head[m.end():]
    vm = re.match(r"\s*[-:–—]?\s*(?:\n\s*)*([^\n]+)", rest)
    if not vm:
        return None
    val = re.sub(r"^[\s\-:–—]+", "", vm.group(1))
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


def extract_aircraft(text):
    return _labelled(text, _AIRCRAFT_LABEL_RE)


def extract_location(text):
    return _labelled(text, _LOCATION_LABEL_RE)


def extract_operator(text):
    return _labelled(text, _OPERATOR_LABEL_RE)


def extract_registration(text):
    """Try to find registration from cover block label."""
    val = _labelled(text, _REG_LABEL_RE)
    if val:
        # Take first token that looks like a registration
        tok = val.split()[0] if val.split() else val
        return tok.upper() if len(tok) >= 3 else None
    return None


# ── HTTP client ──────────────────────────────────────────────────────────────

def make_client():
    """Return an httpx.Client with browser UA."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ── Discovery: enumerate accident PDFs ──────────────────────────────────────

def discover_pdf_urls(client):
    """Return a de-duplicated list of (source_url, title) for accident PDFs.

    Strategy:
    1. Parse the accident-investigations page HTML for any direct PDF <a> hrefs.
    2. Query WP REST API media endpoint with several search terms.

    URLs are de-duplicated and filtered through _is_accident_pdf().
    """
    seen = set()
    results = []

    # Step 1: page HTML
    try:
        print("[jcaa discover] fetching index page ...")
        resp = client.get(INDEX_URL)
        resp.raise_for_status()
        html = resp.text
        for m in re.finditer(r'href="(https?://[^"]+\.pdf)"', html, re.IGNORECASE):
            url = m.group(1)
            if url not in seen and _is_accident_pdf(url):
                seen.add(url)
                results.append((url, ""))
    except Exception as exc:
        print(f"[jcaa discover] index page error: {exc}")

    # Step 2: WP REST API media search
    for term in _MEDIA_SEARCH_TERMS:
        time.sleep(DELAY)
        try:
            print(f"[jcaa discover] WP media search: {term!r} ...")
            resp = client.get(
                WP_MEDIA_API,
                params={
                    "search": term,
                    "per_page": 100,
                    "mime_type": "application/pdf",
                    "_fields": "source_url,title",
                },
            )
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list):
                print(f"[jcaa discover] unexpected response for {term!r}: {items}")
                continue
            for item in items:
                url = item.get("source_url", "")
                title = item.get("title", {}).get("rendered", "") if isinstance(item.get("title"), dict) else ""
                if url and url not in seen and _is_accident_pdf(url, title):
                    seen.add(url)
                    results.append((url, title))
        except Exception as exc:
            print(f"[jcaa discover] WP media {term!r} error: {exc}")

    return results


def download(client, pdf_url, dest):
    """GET pdf_url and write to dest. Raises on error."""
    resp = client.get(pdf_url, headers={"Referer": INDEX_URL})
    resp.raise_for_status()
    Path(dest).write_bytes(resp.content)
