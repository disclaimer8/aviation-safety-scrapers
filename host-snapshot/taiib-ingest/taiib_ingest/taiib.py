# taiib_ingest/taiib.py
"""TAIIB (Latvia) HTML scraper: single-page accordion listing discovery and
PDF download.

Source: https://www.taiib.gov.lv/lv/aviacijas-nobeiguma-zinojumi-0
  ⚠️ The trailing "-0" is mandatory; the obvious URL without it 302-redirects.

The "aviacijas-nobeiguma-zinojumi" page ("aviation final reports") is a single
server-rendered Drupal page.  Reports are grouped into per-year accordion cards;
each report row consists of a descriptive <p> paragraph (Latvian or English)
followed by a media-download anchor:

    <a href="/lv/media/<id>/download?attachment"
       type="application/pdf; length=..." title="<filename>.pdf" ...>...</a>

There is no per-report HTML detail page and no direct .pdf href — the PDF is
served via the Drupal media-download endpoint.

case_id is INTRINSIC and order-independent:
  • report reference ("Nr. 4-02/2-24(2-25)" / "No.4-02/8-12") when present,
  • else registration + ISO event-date,
  • else event-date, else a bare media-id placeholder.
A stable media-id suffix is appended only to break a base collision (the
media-id is part of the row's permanent download URL, so this stays
order-independent).
"""
import html as _html
import re
from pathlib import Path

BASE = "https://www.taiib.gov.lv"
INDEX_URL = BASE + "/lv/aviacijas-nobeiguma-zinojumi-0"
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
# Month tables
# ──────────────────────────────────────────────
# Latvian month roots (declined forms vary: maijā/maija, oktobra/oktobrī, …)
_LV_MONTHS = {
    "janvār": 1, "februār": 2, "mart": 3, "aprīl": 4, "april": 4, "maij": 5,
    "jūnij": 6, "junij": 6, "jūlij": 7, "julij": 7, "august": 8,
    "septembr": 9, "oktobr": 10, "novembr": 11, "decembr": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Media-download anchor with a PDF title attribute.
_ANCHOR_RE = re.compile(
    r'<a\s+href="(/lv/media/(\d+)/download\?attachment)"[^>]*?title="([^"]*)"',
    re.IGNORECASE,
)

# Paragraph blocks (for nearest preceding description).
_P_RE = re.compile(r"<p>(.*?)</p>", re.DOTALL | re.IGNORECASE)

# Latvian date: "2024. gada 4. maijā" / "2018. gada 16. oktobra"
_LV_DATE_RE = re.compile(
    r"(\d{4})\.\s*gada\s+(\d{1,2})\.\s*([a-zāēīūļņģķčšž]+)", re.IGNORECASE
)
# English date: "March 8, 2023" / "NOVEMBER 13,2012" (comma optional space)
_EN_DATE_MDY_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})")
# English date: "8 August 2021"
_EN_DATE_DMY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")

# Registration mark, preferring those after a registration keyword.
_REG_KW_RE = re.compile(
    r"(?:reģistr\w*|registr\w*|registered)\s*(?:Nr\.?|nr\.?|No\.?)?\s*"
    r"([A-Z]{1,2}-[A-Z0-9]{2,6}|N\d{1,5}[A-Z]{0,2}|RA-\d{3,5}[A-Z]?)",
    re.IGNORECASE,
)
# Bare registration fallback (YL-, LY-, SE-, VP-, PH-, OH-, LN-, N12345, RA-…)
_REG_BARE_RE = re.compile(
    r"\b([A-Z]{1,2}-[A-Z0-9]{2,5}|N\d{1,5}[A-Z]{0,2}|RA-\d{3,5}[A-Z]?)\b"
)

# Report reference: "Nr. 4-02/2-24(2-25)" / "No.4-02/8-12/5-13" / "No 4-02-9/2012"
_REF_RE = re.compile(r"\bN[ro]\.?\s*([0-9][0-9/\-()]+[0-9)])")

# Event-class keyword detection (Latvian + English).
_ACCIDENT_RE = re.compile(r"nelaimes gad|\baccident\b", re.IGNORECASE)
_SERIOUS_RE = re.compile(r"nopietn\w*\s+incident|serious incident", re.IGNORECASE)
_INCIDENT_RE = re.compile(r"incident", re.IGNORECASE)

_NONSLUG = re.compile(r"[^a-z0-9]+")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ──────────────────────────────────────────────
# Field parsers
# ──────────────────────────────────────────────

def _normalize_case_id(s: str) -> str:
    """Lowercase, collapse any non [a-z0-9] run to '-', strip edge '-'."""
    return _NONSLUG.sub("-", (s or "").lower()).strip("-")


def parse_date(text: str) -> str | None:
    """Parse a Latvian or English date string → ISO YYYY-MM-DD, or None."""
    if not text:
        return None
    t = text.lower()
    m = _LV_DATE_RE.search(t)
    if m:
        year, day, mon = int(m.group(1)), int(m.group(2)), m.group(3)
        for root, num in _LV_MONTHS.items():
            if mon.startswith(root):
                return _iso(year, num, day)
    m = _EN_DATE_MDY_RE.search(t)
    if m and m.group(1) in _EN_MONTHS:
        return _iso(int(m.group(3)), _EN_MONTHS[m.group(1)], int(m.group(2)))
    m = _EN_DATE_DMY_RE.search(t)
    if m and m.group(2) in _EN_MONTHS:
        return _iso(int(m.group(3)), _EN_MONTHS[m.group(2)], int(m.group(1)))
    return None


def _iso(y, mo, d):
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_registration(text: str) -> str | None:
    """Extract an aircraft registration, preferring a keyword-anchored mark."""
    if not text:
        return None
    m = _REG_KW_RE.search(text)
    if m:
        return m.group(1).upper()
    m = _REG_BARE_RE.search(text)
    return m.group(1).upper() if m else None


def parse_reference(text: str) -> str | None:
    """Extract a report reference number ('Nr. …' / 'No. …'), or None."""
    if not text:
        return None
    m = _REF_RE.search(text)
    return m.group(1) if m else None


def classify_event(text: str) -> str:
    """Classify event_class from description text (LV + EN heuristics)."""
    t = text or ""
    if _SERIOUS_RE.search(t):
        return "Serious incident"
    if _ACCIDENT_RE.search(t):
        return "Accident"
    if _INCIDENT_RE.search(t):
        return "Incident"
    return "Report"


def detect_lang(text: str) -> str | None:
    """Heuristic: Latvian if it contains Latvian diacritics / keywords."""
    if not text:
        return None
    if re.search(r"[āēīūļņģķčšž]", text.lower()):
        return "lv"
    if re.search(r"reģistr|aviācij|gaisa kuģi|nelaimes|nopietn", text.lower()):
        return "lv"
    if re.search(r"\bfinal report\b|registration|serious incident|accident",
                 text.lower()):
        return "en"
    return "lv"


def make_case_id(reference, registration, date_iso, media_id) -> str:
    """
    Build an INTRINSIC, order-independent case_id base.

    Priority: reference → registration+date → date → media-id placeholder.
    The caller is responsible for appending a media-id suffix to break any
    cross-row base collision.
    """
    if reference:
        return _normalize_case_id(reference)
    if registration and date_iso:
        return _normalize_case_id(f"{registration}-{date_iso}")
    if registration:
        return _normalize_case_id(f"{registration}") + f"-m{media_id}"
    if date_iso:
        return _normalize_case_id(f"taiib-{date_iso}") + f"-m{media_id}"
    return f"m{media_id}"


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def _nearest_description(html: str, anchor_start: int) -> str:
    """Return the nearest non-trivial <p> text preceding anchor_start."""
    best = ""
    for pm in _P_RE.finditer(html, 0, anchor_start):
        txt = _html.unescape(_TAG_SUB(pm.group(1)))
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt and len(txt) > 15:
            best = txt
    return best


def _TAG_SUB(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s)


def parse_listing(html: str, year_url: str = "") -> list[dict]:
    """
    Parse the TAIIB single listing page → list of report dicts.

    Each dict has:
      case_id            str   intrinsic, normalized, unique
      report_url         str   the listing-page URL (with media anchor)
      pdf_url            str   absolute media-download URL
      pdf_url_es         None  (parity with template; unused here)
      pdf_url_en         None
      title              str   the report filename (from the anchor title attr)
      event_class        str
      aircraft           None  (extracted later from the PDF; not in listing)
      registration       str|None
      date_of_occurrence str|None  ISO YYYY-MM-DD
      location           None
      lang               str|None
      reference          str|None

    Rows without a parseable case_id base get a media-id placeholder, so every
    PDF row is captured.  Base collisions are broken with the intrinsic media-id.
    """
    raw_rows: list[dict] = []
    for m in _ANCHOR_RE.finditer(html):
        media_url = BASE + m.group(1)
        media_id = m.group(2)
        filename = _html.unescape(m.group(3)).strip()

        desc = _nearest_description(html, m.start())
        date_iso = parse_date(desc) or parse_date(filename.replace(".", " "))
        registration = parse_registration(desc)
        reference = parse_reference(desc)
        event_class = classify_event(desc)
        lang = detect_lang(desc)
        base = make_case_id(reference, registration, date_iso, media_id)

        raw_rows.append({
            "case_id": base,
            "media_id": media_id,
            "report_url": year_url or INDEX_URL,
            "pdf_url": media_url,
            "pdf_url_es": None,
            "pdf_url_en": None,
            "title": filename,
            "event_class": event_class,
            "aircraft": None,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": None,
            "lang": lang,
            "reference": reference,
        })

    # Break base collisions with the intrinsic media-id (order-independent).
    from collections import Counter
    counts = Counter(r["case_id"] for r in raw_rows)
    rows: list[dict] = []
    for r in raw_rows:
        if counts[r["case_id"]] > 1:
            r = dict(r)
            r["case_id"] = f"{r['case_id']}-m{r['media_id']}"
        rows.append(r)
    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url with Referer header and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
