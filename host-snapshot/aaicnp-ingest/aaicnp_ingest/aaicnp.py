# aaicnp_ingest/aaicnp.py
"""Nepal AAIC (Aircraft Accident Investigation Commission, under CAAN) scraper.

Source: Civil Aviation Authority of Nepal (caanepal.gov.np).

The Nepal final reports are NOT exposed through a single clean HTML index.
They are SCATTERED:
  * most final-report PDFs live on the gov CDN  giwmscdnone.gov.np/media/pdf_upload/
  * some live on  caanepal.gov.np/storage/app/media/...
  * a chronological accident/incident *record list* (registration + date, no
    report links) lives on the CAAN Safety Management Division page.

Discovery therefore harvests the *actual hrefs* from a fixed set of CAAN
listing surfaces (SMD accidents/incidents, SMD documents, all-news + the
news-detail posts they link to, all-notice + notice-detail pages) and keeps
every PDF href whose anchor text / filename signals an aircraft accident or
serious-incident *final / investigation report*, FOLLOWING the href to
whatever host it points at.  A small SEED list of known gov/CDN final-report
URLs guarantees coverage of CDN-only cases that no current CAAN page links.

case_id is INTRINSIC (order-independent): derived from the report reference
("Aircraft Accident Investigation Report N/YYYY") when present in the PDF text,
else from registration + event date, else from a normalised filename slug.

English text-layer PDFs.  Throttle is conservative (gov.np can be slow).
"""
import html as _html
import re
from pathlib import Path

BASE = "https://caanepal.gov.np"
CDN = "https://giwmscdnone.gov.np"
INDEX_URL = BASE + "/safety/safety-management-division/accidentsincidents"
REFERER = BASE + "/"
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
# Listing surfaces walked during discovery
# ──────────────────────────────────────────────

# Static CAAN pages that can carry accident-report PDF hrefs.
STATIC_LISTING_URLS = [
    BASE + "/safety/safety-management-division/accidentsincidents",
    BASE + "/safety/safety-management-division/documents",
    BASE + "/safety/safety-management-division/activities",
]

# Paginated post listings; each page links out to post/notice detail pages
# whose bodies may embed the report PDF.
ALL_NEWS_URL = BASE + "/all-news"
ALL_NOTICE_URL = BASE + "/all-notice"
MAX_LIST_PAGES = 30  # safety ceiling; the loop stops early on an empty/repeating page

# Known gov/CDN final-report PDFs (seed) — guarantees CDN-only cases are found
# even when no current CAAN page links them.  Gov/CDN copies only (never ASN).
SEED_REPORT_URLS = [
    CDN + "/media/pdf_upload/REPORT%20OF%209N-AMI%20H125%20(AS350%20B3e)"
        "%20Helicopter%20Owned%20&%20Operated%20by%20Air%20Dynasty%20Heli"
        "%20Services%20Pvt.%20Ltd.%20at%20near%20SisneVir,%20Pathivara,"
        "%20Nepal_m9uvgmg.pdf",
    CDN + "/media/pdf_upload/Final%20Report%20-%2023%20March%202026"
        "%20(1)_fkdtbdw.pdf",
]

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# A PDF anchor whose text/filename signals an aircraft accident/incident report.
_REPORT_KEYWORDS = re.compile(
    r"(accident|incident|investigation|final\s*report|9n-|aaic)",
    re.IGNORECASE,
)
# Anchors we never want even if they match a keyword (registry index, manuals…).
_REPORT_NEGATIVE = re.compile(
    r"(records?\s+of\s+nepalese|aeroplane-accident|helicopter-accident|"
    r"procedure\s+manual|safety\s+report|prelim(?:inary)?|"
    r"safety\s+management|annual|policy|directive|requirement|circular|"
    r"surveillance|enforcement|state\s+safety|exemption|syllabus)",
    re.IGNORECASE,
)

# Post/notice detail links from the paginated listings.
_POST_LINK_RE = re.compile(
    r'href="(' + re.escape(BASE) + r'/(?:news-detail/post|notice-details)/[^"]+)"',
    re.IGNORECASE,
)

# Report reference inside PDF text: "Aircraft Accident Investigation Report 1/2026"
_REF_RE = re.compile(
    r"Aircraft\s+Accident\s+Investigation\s+Report\s+(\d{1,3})\s*/\s*(\d{4})",
    re.IGNORECASE,
)
# Registration detection (deterministic, order-independent).
#
# Aircraft *type* designators (AS-350, B-737, DG-1000, H-125) share the generic
# "<letters>-<chars>" shape and frequently PRECEDE the registration in a report
# header, so a naive first-match grabs the type code as the registration.  Two
# guards: (1) always prefer a Nepali 9N- registration when one exists anywhere
# in the header; (2) the generic foreign-reg fallback rejects candidates whose
# post-hyphen part is all-digits (737, 350, 125, 1000 …) — real registrations
# carry letters (VT-ABC, B-LXA), type designators are alphanumeric model codes.
_REG_9N_RE = re.compile(r"\b9N[\s-]?[A-Z]{2,4}\b")
_REG_GENERIC_RE = re.compile(r"\b([A-Z]{1,2}-[A-Z0-9]{2,5})\b")
# Combined pattern retained for any external callers / back-compat.
_REG_RE = re.compile(r"\b(9N[\s-]?[A-Z]{2,4}|[A-Z]{1,2}-[A-Z0-9]{2,5})\b")


def _find_registration(text):
    """Return the aircraft registration from report text, or None.

    Prefers a Nepali ``9N-`` registration anywhere in the text; otherwise falls
    back to the first generic hyphenated registration whose post-hyphen segment
    contains a letter (rejecting all-digit aircraft type designators such as
    AS-350 / B-737 / H-125 / DG-1000).  Deterministic — never order-dependent.
    """
    if not text:
        return None
    m = _REG_9N_RE.search(text)
    if m:
        return re.sub(r"\s+", "-", m.group(0).upper())
    for gm in _REG_GENERIC_RE.finditer(text):
        cand = gm.group(1).upper()
        suffix = cand.split("-", 1)[1]
        if not suffix.isdigit():  # reject all-digit type designators
            return cand
    return None
# Dates in the PDF text: "On 29th October 2025", "February 27, 2019"
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_DATE_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b"
)
_DATE_MDY_RE = re.compile(
    r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\b"
)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and Referer."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# case_id normalisation (INTRINSIC, order-independent)
# ──────────────────────────────────────────────

def _normalize_case_id(raw):
    """Normalise an intrinsic case_id key.

    Uppercases, collapses whitespace, maps any run of non [A-Z0-9/] to a single
    hyphen, trims stray separators.  Deterministic for a given input — NEVER
    encounter-order-dependent.
    """
    if not raw:
        return ""
    s = _WS_RE.sub(" ", str(raw)).strip().upper()
    s = re.sub(r"[^A-Z0-9/]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def make_case_id(*, ref=None, registration=None, event_date=None, filename=None):
    """Build an INTRINSIC case_id from the strongest signal available.

    Priority (all order-independent):
      1. report reference   -> "AAIC-NP-<N>-<YYYY>"   (e.g. AAIC-NP-1-2026)
      2. registration+date  -> "<REG>-<YYYY-MM-DD>"   (e.g. 9N-AMI-2019-02-27)
      3. registration only  -> "<REG>"
      4. filename slug      -> normalised filename stem
    """
    if ref:
        num, year = ref
        return _normalize_case_id(f"AAIC-NP-{int(num)}-{int(year)}")
    if registration and event_date:
        return _normalize_case_id(f"{registration}-{event_date}")
    if registration:
        return _normalize_case_id(registration)
    if filename:
        stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
        return _normalize_case_id(stem)[:80]
    return ""


# ──────────────────────────────────────────────
# Text helpers
# ──────────────────────────────────────────────

def _anchor_text(raw_inner):
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub("", raw_inner))).strip()


def _abs_url(href):
    href = _html.unescape(href).strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    return href


def _is_pdf(url):
    return ".pdf" in url.lower()


def _filename_of(url):
    from urllib.parse import urlparse, unquote
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1])


def looks_like_report(url, anchor_text=""):
    """True when a PDF href looks like an aircraft accident/incident report."""
    if not _is_pdf(url):
        return False
    blob = f"{_filename_of(url)} {anchor_text}"
    if _REPORT_NEGATIVE.search(blob):
        return False
    return bool(_REPORT_KEYWORDS.search(blob))


# ──────────────────────────────────────────────
# Anchor harvesting
# ──────────────────────────────────────────────

def harvest_report_links(page_html):
    """Return list of (url, anchor_text) for report-PDF hrefs on a page.

    De-duplicated by URL, order preserved.
    """
    out = []
    seen = set()
    for m in _HREF_RE.finditer(page_html or ""):
        url = _abs_url(m.group(1))
        text = _anchor_text(m.group(2))
        if not looks_like_report(url, text):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, text))
    return out


def iter_post_links(listing_html):
    """Return news-detail / notice-detail URLs found on a listing page."""
    seen = set()
    out = []
    for m in _POST_LINK_RE.finditer(listing_html or ""):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ──────────────────────────────────────────────
# PDF-text metadata extraction
# ──────────────────────────────────────────────

def _parse_date(text):
    """First plausible date in the report text -> ISO YYYY-MM-DD, or None."""
    for rx, order in ((_DATE_DMY_RE, "dmy"), (_DATE_MDY_RE, "mdy")):
        for m in rx.finditer(text):
            if order == "dmy":
                d, mon, y = m.group(1), m.group(2), m.group(3)
            else:
                mon, d, y = m.group(1), m.group(2), m.group(3)
            mo = _MONTHS.get(mon.lower())
            if not mo:
                continue
            try:
                di, yi = int(d), int(y)
            except ValueError:
                continue
            if 1 <= di <= 31 and 1900 <= yi <= 2100:
                return f"{yi:04d}-{mo:02d}-{di:02d}"
    return None


def extract_metadata(report_text, filename=""):
    """Pull intrinsic metadata from the report PDF text (and filename).

    Returns dict: case_id, registration, event_date, title, event_class,
    aircraft (None — model is rarely reliably parseable), report_ref.
    """
    head = (report_text or "")[:4000]

    ref_m = _REF_RE.search(head)
    ref = (ref_m.group(1), ref_m.group(2)) if ref_m else None

    registration = _find_registration(head)

    event_date = _parse_date(head)

    # event_class from text
    low = head.lower()
    if "serious incident" in low:
        event_class = "Serious incident"
    elif "incident" in low and "accident" not in low[:200]:
        event_class = "Incident"
    else:
        event_class = "Accident"

    # title: first non-empty meaningful line of the report
    title = None
    for line in (report_text or "").splitlines():
        s = line.strip()
        if len(s) >= 12:
            title = s
            break

    case_id = make_case_id(
        ref=ref, registration=registration, event_date=event_date,
        filename=filename,
    )
    return {
        "case_id": case_id,
        "registration": registration,
        "event_date": event_date,
        "title": title,
        "event_class": event_class,
        "aircraft": None,
        "report_ref": f"{ref[0]}/{ref[1]}" if ref else None,
    }


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url, dest):
    """GET pdf_url with Referer header and write bytes to dest.

    Raises on non-2xx responses (httpx) so the pipeline keeps the row at 'new'.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
