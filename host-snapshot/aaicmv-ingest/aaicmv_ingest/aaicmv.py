# aaicmv_ingest/aaicmv.py
"""AAIC Maldives (Accident Investigation Coordinating Committee) HTML scraper.

Source: https://caa.gov.mv/accidents-incidents
- A single server-rendered page (Maldives CAA site) with one <table> whose body
  rows are the official AICC accident/incident reports.
- Each <tr> has three <td>:
    1. "<EventClass>: <a href='/attachments/X.pdf'>REF</a>"  (e.g. "Accident: 2024/03")
    2. event date (e.g. "13 October 2024")
    3. details / title (English: aircraft, registration, location, "Final"/"Preliminary")
- PDFs live under /attachments/<token>.pdf and are English final / preliminary
  reports. Download needs a browser UA + Referer.

case_id is INTRINSIC: normalised report reference + report-type discriminator
(final/prelim/report) derived from the details text. The same reference often has
both a Final and a Preliminary document; the type suffix keeps them distinct
WITHOUT any encounter-order suffix.
"""
import html as _html
import re
import datetime

BASE = "https://caa.gov.mv"
INDEX_URL = BASE + "/accidents-incidents"
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
# Constants
# ──────────────────────────────────────────────

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_PDF_HREF_RE = re.compile(r"""href=["']([^"']+\.pdf)["']""", re.IGNORECASE)

# Event date: "DD Month YYYY" anywhere in the date cell (tolerate "1st", "02nd").
_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)
# Registration: 8Q-XXX (Maldivian) or foreign marks (e.g. 9M-XXC, VT-XXX).
_REG_RE = re.compile(r"\b([0-9]?[A-Za-z]{1,2}-[A-Za-z0-9]{2,5})\b")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ──────────────────────────────────────────────
# case_id (intrinsic, deterministic, no encounter-order suffix)
# ──────────────────────────────────────────────

def _normalize_case_id(ref: str) -> str:
    """Normalise a report reference to 'YYYY-NN'.

    Accepts variants like '2024/03', '2020-01', '2024/03/P', 'P2020/04'.
    Returns the first 4-digit year and the next number zero-padded to 2 digits.
    Falls back to a slug of the ref when it lacks two number groups.
    """
    ref = (ref or "").strip()
    nums = re.findall(r"\d+", ref)
    if len(nums) >= 2:
        year = nums[0]
        seq = int(nums[1])
        return f"{year}-{seq:02d}"
    cleaned = re.sub(r"[^0-9A-Za-z]+", "-", ref).strip("-").lower()
    return cleaned or "unknown"


def _report_type(ref: str, details: str) -> str:
    """Determine report type from details text (and ref prelim markers).

    Returns 'final', 'prelim', or 'report'.
    """
    dl = (details or "").lower()
    if "final" in dl:
        return "final"
    if "prelim" in dl:
        return "prelim"
    # Ref-level preliminary markers: trailing '/P' or leading 'P'.
    r = (ref or "").strip()
    if r.upper().endswith("/P") or re.match(r"^P\d", r, re.IGNORECASE):
        return "prelim"
    return "report"


def make_case_id(ref: str, details: str) -> str:
    """Build the intrinsic case_id: 'MV-<normref>-<type>'.

    e.g. ref='2024/03', details='Final report ...' -> 'MV-2024-03-final'.
    Deterministic from the row's own content; collision-free across the listing
    because the (reference, report-type) pair is unique per published document.
    """
    return f"MV-{_normalize_case_id(ref)}-{_report_type(ref, details)}"


# ──────────────────────────────────────────────
# Row parsing helpers
# ──────────────────────────────────────────────

def _cell_text(td_html: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", td_html))).strip()


def _parse_date(date_cell: str):
    """Parse 'DD Month YYYY' -> ISO 'YYYY-MM-DD', or None."""
    m = _DATE_RE.search(date_cell or "")
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTHS_EN.get(m.group(2).lower())
    year = int(m.group(3))
    if not month:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


_AIRCRAFT_HINTS = re.compile(
    r"(DHC-?\d[\w-]*|Viking Air[\w .-]*|ATR[\s-]?\d[\w-]*|Cessna[\w .-]*|"
    r"Airbus[\w .-]*|A3\d{2}[\w-]*|Boeing[\w .-]*|B7\d{2}[\w-]*|"
    r"De Havilland[\w .-]*)",
    re.IGNORECASE,
)


def _parse_aircraft(details: str):
    m = _AIRCRAFT_HINTS.search(details or "")
    if m:
        return m.group(1).strip().rstrip(",.")
    return None


def _parse_registration(details: str):
    m = _REG_RE.search(details or "")
    return m.group(1).upper() if m else None


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    """Parse the accidents-incidents page -> list of report dicts.

    Each dict:
      case_id            str   intrinsic, e.g. 'MV-2024-03-final'
      ref                str   raw reference token from the table
      pdf_url            str|None  absolute PDF URL
      event_class        str   'Accident' | 'Serious incident' | 'Incident'
      report_type        str   'final' | 'prelim' | 'report'
      aircraft           str|None
      registration       str|None
      date_of_occurrence str|None  ISO
      location           str|None  (not separately parsed; left None)
      title              str   full details text

    Rows lacking a PDF link are skipped (header row, narrative-only rows).
    De-duplicates on case_id, keeping the first occurrence.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    for tr_m in _TR_RE.finditer(html):
        tds = _TD_RE.findall(tr_m.group(1))
        if len(tds) < 3:
            continue

        href_m = _PDF_HREF_RE.search(tr_m.group(1))
        if not href_m:
            continue  # header row / no report attached

        col1 = _cell_text(tds[0])
        date_cell = _cell_text(tds[1])
        details = _cell_text(tds[2])

        # ref = text after the "Label:" prefix in col1; label = event class.
        if ":" in col1:
            label, ref = col1.split(":", 1)
            label = label.strip()
            ref = ref.strip()
        else:
            label = ""
            ref = col1.strip()

        report_type = _report_type(ref, details)
        case_id = make_case_id(ref, details)
        if case_id in seen:
            continue
        seen.add(case_id)

        # event_class: prefer label; refine "Incident" -> "Serious incident"
        # when details mention a serious incident.
        dl = details.lower()
        if label.lower().startswith("accident"):
            event_class = "Accident"
        elif "serious incident" in dl:
            event_class = "Serious incident"
        elif label.lower().startswith("incident") or "incident" in dl:
            event_class = "Incident"
        else:
            event_class = label or "Accident"

        href = href_m.group(1)
        pdf_url = href if href.startswith("http") else BASE + href

        rows.append({
            "case_id": case_id,
            "ref": ref,
            "pdf_url": pdf_url,
            "event_class": event_class,
            "report_type": report_type,
            "aircraft": _parse_aircraft(details),
            "registration": _parse_registration(details),
            "date_of_occurrence": _parse_date(date_cell),
            "location": None,
            "title": details,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest) -> None:
    """GET pdf_url with Referer header and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
