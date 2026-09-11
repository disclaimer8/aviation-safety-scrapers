# aaisl_ingest/aaisl.py
"""AAII Sri Lanka (CAA Sri Lanka) HTML scraper.

Source: https://www.caa.lk/en/aircraft-accident-and-incidents
- Single static, server-rendered English listing page (~37 PDF reports).
- Layout: an HTML table of paired <td> cells.  Each report = a metadata cell
  (event class + 'Date:' + 'Location:' + 'Operator:') immediately followed by
  a PDF-link cell whose anchor text is the descriptive report title.
- The page HTML is mildly malformed in places (one merged <tr>, a stray anchor
  splitting the word "INCIDENT").  Parsing over the flat <td> cell stream
  (rather than <tr> blocks) is robust to that.
- case_id is INTRINSIC: registration + event-date when both are known, else a
  slug of the descriptive anchor title.  No encounter-order suffixes.

TSIB-rehost dedup:
  One listed PDF (4R-ABN, Changi 2019) is actually a Transport Safety
  Investigation Bureau (Singapore) report re-hosted by CAA Sri Lanka.  We must
  NOT ingest it under the Sri Lanka source — it would duplicate our existing
  `tsib` source.  Detection happens at parse time on the extracted PDF text via
  is_foreign_authority(); the issuing authority of a genuine AAII report is the
  "Civil Aviation Authority of Sri Lanka".
"""
import datetime
import html as _html
import re
from pathlib import Path

BASE = "https://www.caa.lk"
INDEX_URL = BASE + "/en/aircraft-accident-and-incidents"
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
# Constants / regexes
# ──────────────────────────────────────────────

# Accident report PDFs live under these path fragments; everything else on the
# page (org charts, agreements, Boeing stats, etc.) is excluded.
_REPORT_PATH_RE = re.compile(
    r"(?:/accident_investigation_unit/Accident_Reports_New/|/\d{4}_[A-Za-z]+/Final_report)",
    re.IGNORECASE,
)

_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)

_CLASS_RE = re.compile(r"\b(SERIOUS\s+INCIDENT|ACCIDENT|INCIDENT)\b", re.IGNORECASE)
_DATE_LABEL_RE = re.compile(r"Date\s*:\s*", re.IGNORECASE)
_LOCATION_RE = re.compile(r"Location\s*:\s*(.*?)(?:Operators?\s*:|$)", re.IGNORECASE | re.DOTALL)
_OPERATOR_RE = re.compile(r"Operators?\s*:\s*(.*?)$", re.IGNORECASE | re.DOTALL)

# Date in the metadata cell: usually 'YYYY.MM.DD', occasionally 'DD - Mon -YYYY'.
_DATE_DOTTED_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
# Anchor / metadata textual date: 'Nth Month YYYY' or 'Month YYYY'.
_DATE_TEXT_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:of\s+)?[-\s]*([A-Za-z]+)\.?\s*[-\s]*(\d{4})"
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Registration marks: Sri Lankan 4R-XXX plus common foreign prefixes that
# appear in the listing (HZ-, TF-, PH-, RA-, G-, N…, A7-, etc.).
_REG_RE = re.compile(
    r"\b("
    r"4R-?[A-Z]{2,4}"
    r"|HZ-[A-Z]{2,4}|TF-[A-Z]{2,4}|PH-[A-Z]{2,4}|G-[A-Z]{2,4}"
    r"|A\d-[A-Z]{2,4}|9V-[A-Z]{2,4}|VT-[A-Z]{2,4}"
    r"|RA-?\d{4,5}|N\d{1,5}[A-Z]{0,2}"
    r")\b"
)

# Aircraft type heuristics from descriptive text.
_AIRCRAFT_RE = re.compile(
    r"\b("
    r"Cessna\s*\d{2,3}[A-Z]?|Airbus\s*A?\d{3}(?:-\d{2,3})?|Boeing\s*B?\d{3}(?:-\d{2,3}[A-Z]{0,3})?"
    r"|DHC-?\d[A-Z]?|HS\s*\d{3}|AN-?\d{1,3}|DC-?\d(?:-\d{2,3}[A-Z]?)?|F[- ]?27|ATR\s*\d{2}"
    r")\b",
    re.IGNORECASE,
)

# Issuing-authority signatures used by is_foreign_authority().
# Whitespace-tolerant: the phrase is frequently split across a line break in the
# extracted PDF text (e.g. "Civil Aviation\nAuthority of Sri Lanka").  Some AAII
# reports are technically investigated by a foreign AAIB but are still RELEASED
# by CAA Sri Lanka -- that release line marks them as genuine AAII publications.
_SL_AUTHORITY_RE = re.compile(
    r"Civil\s+Aviation\s+Authority\s+of\s+Sri\s+Lanka", re.IGNORECASE
)
# Known foreign air-safety investigation authorities that may be re-hosted.
_FOREIGN_AUTHORITY_RE = re.compile(
    r"Transport Safety Investigation Bureau"
    r"|\bTSIB\b"
    r"|Air Accidents? Investigation Branch"            # UK AAIB
    r"|National Transportation Safety Board"           # US NTSB
    r"|Bureau d['e’]Enqu[eê]tes"                  # FR BEA
    r"|Australian Transport Safety Bureau"             # AU ATSB
    r"|Aircraft Accident Investigation Bureau",        # generic AAIB (non-LK)
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

import os as _os

# www.caa.lk serves an INCOMPLETE TLS chain (Sectigo R36 intermediate + R46 root
# are not sent and are absent from most trust stores), so both curl and the
# default certifi bundle fail with "unable to get local issuer certificate".
# We ship the missing intermediate + root alongside the package and point httpx
# at a merged bundle.  (Cf. the AAIU TLS-intermediate trap.)
_CA_BUNDLE = _os.path.join(_os.path.dirname(__file__), "caa_lk_bundle.pem")


def ca_bundle():
    """Return the shipped CA bundle path if present, else True (system default)."""
    return _CA_BUNDLE if _os.path.exists(_CA_BUNDLE) else True


def make_client():
    """Return an httpx.Client configured with browser UA and the CAA-LK bundle."""
    import httpx
    return httpx.Client(
        headers=HEADERS, follow_redirects=True, timeout=30.0, verify=ca_bundle()
    )


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(s):
    """Normalize a raw case_id to upper-case, single-dash-separated form."""
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Z0-9-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def make_case_id(registration, date_iso, title):
    """Build an INTRINSIC case_id.

    Preference order (deterministic, no encounter-order suffix):
      1. '<REG>-<YYYYMMDD>' when both registration and date are known.
      2. '<REG>' when only registration is known.
      3. 'LK-<slug-of-title>' when no registration (fallback).
    """
    reg = _normalize_case_id(registration) if registration else None
    ymd = date_iso.replace("-", "") if date_iso else None
    if reg and ymd:
        return f"{reg}-{ymd}"
    if reg:
        return reg
    from .text import slugify
    slug = slugify(title or "")
    slug = slug[:80].strip("-")
    base = f"LK-{slug}" if slug else "LK-UNKNOWN"
    return _normalize_case_id(base)


# ──────────────────────────────────────────────
# Date parsing
# ──────────────────────────────────────────────

def _parse_dotted_date(text):
    m = _DATE_DOTTED_RE.search(text or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _parse_text_date(text):
    m = _DATE_TEXT_RE.search(text or "")
    if not m:
        return None
    d, mon_raw, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    mo = _MONTHS.get(mon_raw)
    if not mo:
        return None
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _parse_date(meta_text, anchor_text):
    """Prefer the dotted 'YYYY.MM.DD' metadata date, then a textual date in
    either the metadata or the anchor description."""
    return (
        _parse_dotted_date(meta_text)
        or _parse_text_date(meta_text)
        or _parse_text_date(anchor_text)
    )


# ──────────────────────────────────────────────
# Metadata cell parsing
# ──────────────────────────────────────────────

def _clean(s):
    s = re.sub(r"<sup\b[^>]*>.*?</sup>", "", s or "", flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_event_class(meta_text):
    m = _CLASS_RE.search(meta_text or "")
    if not m:
        return None
    val = re.sub(r"\s+", " ", m.group(1).strip()).lower()
    if val == "serious incident":
        return "Serious incident"
    if val == "accident":
        return "Accident"
    return "Incident"


def _parse_location(meta_text):
    m = _LOCATION_RE.search(meta_text or "")
    if not m:
        return None
    loc = m.group(1).strip().rstrip(".").strip()
    return loc or None


def _parse_operator(meta_text):
    m = _OPERATOR_RE.search(meta_text or "")
    if not m:
        return None
    op = m.group(1).strip().rstrip(".").strip()
    return op or None


def _is_report_href(href):
    return bool(_REPORT_PATH_RE.search(href))


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_listing(html, base_url=BASE):
    """Parse the AAII Sri Lanka listing page → list of report dicts.

    Walks the flat <td> cell stream.  A report cell is one whose first .pdf link
    is an accident report; its metadata is the most recent preceding non-report
    cell that carries an event class and/or a 'Date:' label.

    Each dict:
      case_id            str   intrinsic (REG-YYYYMMDD / REG / LK-<slug>)
      report_url         None  (AAII has no per-case HTML page)
      pdf_url_en         str   absolute report PDF URL (EN)
      pdf_url_es         None  (single-language source)
      event_class        str|None
      aircraft           str|None
      registration       str|None
      date_of_occurrence str|None  ISO YYYY-MM-DD
      location           str|None
      operator           str|None
      title              str   descriptive anchor text
    """
    cells = _TD_RE.findall(html)
    rows = []
    seen_urls = set()
    prev_meta = None

    for cell in cells:
        report_hrefs = [h for h in _PDF_HREF_RE.findall(cell) if _is_report_href(h)]
        if not report_hrefs:
            # candidate metadata cell?
            if _DATE_LABEL_RE.search(cell) or _CLASS_RE.search(cell):
                prev_meta = cell
            continue

        href = _html.unescape(report_hrefs[0])
        if href.startswith("/"):
            pdf_url = base_url + href
        elif href.startswith("http"):
            pdf_url = href
        else:
            pdf_url = base_url + "/" + href

        if pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)

        meta_text = _clean(prev_meta) if prev_meta else ""
        anchor_text = _clean(cell)

        event_class = _parse_event_class(meta_text)
        location = _parse_location(meta_text)
        operator = _parse_operator(meta_text)
        date_iso = _parse_date(meta_text, anchor_text)

        # registration: prefer anchor description, fall back to metadata
        reg_m = _REG_RE.search(anchor_text) or _REG_RE.search(meta_text)
        registration = reg_m.group(1).upper().replace(" ", "") if reg_m else None
        if registration and registration.startswith("4R") and not registration.startswith("4R-"):
            registration = "4R-" + registration[2:]

        # aircraft type from anchor description
        ac_m = _AIRCRAFT_RE.search(anchor_text)
        aircraft = re.sub(r"\s+", " ", ac_m.group(1)).strip() if ac_m else None

        title = anchor_text

        case_id = make_case_id(registration, date_iso, title)

        rows.append({
            "case_id": case_id,
            "report_url": None,
            "pdf_url_en": pdf_url,
            "pdf_url_es": None,
            "event_class": event_class,
            "aircraft": aircraft,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": location,
            "operator": operator,
            "title": title,
        })

    return rows


# ──────────────────────────────────────────────
# Foreign-authority (TSIB-rehost) detection
# ──────────────────────────────────────────────

def is_foreign_authority(pdf_text):
    """True when the extracted PDF text is a foreign agency's report (e.g. the
    re-hosted TSIB/Singapore report) rather than a CAA Sri Lanka report.

    Heuristic: a foreign-authority signature is present AND the Sri Lanka
    authority signature is absent.  Genuine AAII reports always carry
    'Civil Aviation Authority of Sri Lanka'.
    """
    if not pdf_text:
        return False
    head = pdf_text[:4000]
    if _FOREIGN_AUTHORITY_RE.search(head) and not _SL_AUTHORITY_RE.search(head):
        return True
    return False


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url, dest):
    """GET pdf_url with Referer header and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
