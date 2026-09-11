# bdca_ingest/bdca.py
"""Belize DCA / BAAI (Aircraft Accident Investigation Unit) HTML scraper.

Source: Joomla site with the PhocaDownload component.
  https://www.civilaviation.gov.bz/index.php/accident-investigation-unit-aiu/accident-reports

The "Accident/Incident Reports Downloads" listing is one server-rendered
PhocaDownload category page. Each report is a `<div class="pd-title">` row whose
single <a> points to a gated download URL of the form:

  /index.php/.../accident-reports?download=<id>:<file-slug>

Following that href (with a Referer header) returns the report PDF directly
(Content-Type: application/pdf). There is no separate per-report HTML page.

Titles look like:
  "1991 - Belize Aircraft Accident & Investigation (Final Report N402BL)"
  "2023 - Belize Aircraft Accident & Investigation (Final Report V3-HIN)"
  "1995 - Belize Aircraft Accident & Investigation (Final Report V3-HFD [2])"

case_id is INTRINSIC, derived from year + registration (NOT encounter order):
  BDCA-<YEAR>-<REG>            e.g. BDCA-2023-V3-HIN
  BDCA-<YEAR>-<REG>-<N>        when a "[N]" duplicate marker is present
ENGLISH source; text-layer PDFs for recent years, scanned image PDFs for
older (pre-2000) ones.
"""
import html as _html
import re
from pathlib import Path

BASE = "https://www.civilaviation.gov.bz"
INDEX_URL = (
    BASE
    + "/index.php/accident-investigation-unit-aiu/accident-reports"
)
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

# A PhocaDownload title anchor: capture the download href and the title text.
_TITLE_ANCHOR_RE = re.compile(
    r'<div class="pd-title">\s*<a[^>]*?href="([^"]*\?download=\d+:[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Leading 4-digit year in the title: "1991 - Belize ..."
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# Registration inside "(Final Report <REG>[ [N]])"
# REG: alphanumerics with optional hyphens (V3-HDV, N402BL, N936AN, V3-HFD).
# Optional trailing duplicate marker "[2]".
_REG_RE = re.compile(
    r"\(\s*Final\s+Report\s+([A-Z0-9][A-Z0-9-]*)\s*(?:\[\s*(\d+)\s*\])?\s*\)",
    re.IGNORECASE,
)


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
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(s: str) -> str:
    """Uppercase, collapse internal whitespace to single hyphens, trim."""
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def make_case_id(year, registration, dup=None) -> str:
    """Build an intrinsic case_id from year + registration (+ optional dup marker).

    BDCA-<YEAR>-<REG>           e.g. BDCA-2023-V3-HIN
    BDCA-<YEAR>-<REG>-<dup>     e.g. BDCA-1995-V3-HFD-2
    Falls back gracefully when a part is missing.
    """
    parts = ["BDCA"]
    if year:
        parts.append(str(year))
    if registration:
        parts.append(_normalize_case_id(registration))
    if dup:
        parts.append(str(dup))
    return _normalize_case_id("-".join(parts))


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    """Parse the PhocaDownload accident-reports page → list of report dicts.

    Each dict has:
      case_id            str   e.g. 'BDCA-2023-V3-HIN'
      report_url         str   the gated ?download= page URL (also serves PDF)
      pdf_url            str   same as report_url (download serves the PDF)
      title              str   full title text
      event_class        str   'Accident' (Belize labels these accident reports)
      aircraft           None  (not in listing; comes from PDF if at all)
      registration       str|None
      date_of_occurrence None  (only the year is in the listing → left None)
      location           None
      year               int|None  (kept for reference / case_id)

    Rows whose title yields no registration AND no year are skipped (they
    cannot produce a stable intrinsic case_id).
    """
    rows: list[dict] = []
    seen: set[str] = set()

    for m in _TITLE_ANCHOR_RE.finditer(html):
        href = _html.unescape(m.group(1).strip())
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        title = re.sub(r"\s+", " ", title)

        ym = _YEAR_RE.search(title)
        year = int(ym.group(1)) if ym else None

        rm = _REG_RE.search(title)
        registration = None
        dup = None
        if rm:
            registration = rm.group(1).upper()
            dup = rm.group(2)

        if not registration and not year:
            continue

        case_id = make_case_id(year, registration, dup)
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)

        url = href if href.startswith("http") else BASE + href

        rows.append({
            "case_id": case_id,
            "report_url": url,
            "pdf_url": url,
            "title": title,
            "event_class": "Accident",
            "aircraft": None,
            "registration": registration,
            "date_of_occurrence": None,
            "location": None,
            "year": year,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url with a Referer header and write the bytes to dest.

    The PhocaDownload endpoint serves the PDF directly (application/pdf).
    Raises on non-2xx responses.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
