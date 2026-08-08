# cins_ingest/cins.py
"""CINS (Serbia) HTML scraper: single aviation listing page + PDF download.

Source: https://arhiva.cins.gov.rs/latinica/nesrece-u-vazdusnom-saobracaju.php
  ⚠️ The `www.` host and the `-2.php` variant 404 — use this exact
  arhiva.cins.gov.rs/latinica path.

The page is one server-rendered HTML table.  Each <tr> is a single
investigation row:

    | case_id (link) | aircraft | event type | reg. | date | location | report type |

The site root /doc/vazdusni-saobracaj/ holds ONLY aviation PDFs (rail and
maritime live on sibling pages we never touch), so every table row is in
scope.  Annual summary reports (godisnji-izvestaji) appear OUTSIDE the table
in <p> blocks — parse_listing only walks <tr> rows, so they are excluded.

⚠️ Many PDF hrefs contain literal spaces ("IZVESTAJ 01-08 CESSNA 172G ...").
We URL-encode the href before requesting but keep the scraped href verbatim
in the DB; do NOT reconstruct filenames.
"""
import html as _html
import re

BASE = "https://arhiva.cins.gov.rs"
INDEX_URL = BASE + "/latinica/nesrece-u-vazdusnom-saobracaju.php"
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

# Serbian event-type strings → English report_type.
#   "Udes"            = accident
#   "Ozbiljna nezgoda"= serious incident
#   "Nezgoda"         = incident
_EVENT_MAP = [
    ("ozbiljna nezgoda", "Serious incident"),
    ("nezgoda", "Incident"),
    ("udes", "Accident"),
]

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)
_CASE_ID_RE = re.compile(r"\b(\d{1,2}-\d{2}S?)\b", re.IGNORECASE)
# DD.MM.YYYY (year may be 2- or 4-digit; trailing dot optional)
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Excluded path fragments (annual summaries — never per-case investigations)
_EXCLUDE_FRAGMENTS = ("godisnji-izvestaji",)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA + Referer."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _cell_text(cell_html):
    """Strip tags + collapse whitespace from a <td> body."""
    s = _TAG_RE.sub(" ", cell_html)
    s = _html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _abs_url(href):
    """Resolve a scraped '../doc/...pdf' href to an absolute arhiva URL."""
    h = _html.unescape(href).strip()
    if h.startswith("http"):
        return h
    # strip any number of leading ../  and a leading /
    h = re.sub(r"^(\.\./)+", "/", h)
    if not h.startswith("/"):
        h = "/" + h
    return BASE + h


def make_case_id(anchor_text, cell_text, href):
    """Extract a raw NN-YY[S] case_id.

    Priority: the link cell text (anchor or surrounding cell), then the PDF
    filename.  Returns None if no plausible case_id token is found.
    """
    for src in (anchor_text, cell_text):
        if src:
            m = _CASE_ID_RE.search(src)
            if m:
                return m.group(1)
    # fall back to the filename
    fn = (href or "").split("/")[-1]
    m = _CASE_ID_RE.search(fn)
    return m.group(1) if m else None


def _normalize_case_id(raw):
    """Normalize a case_id to canonical 'NN-YY' form (zero-padded NN, kept S).

    '1-23'   -> '01-23'
    '01-17S' -> '01-17S'
    Returns the upper-cased, padded token, or None.
    """
    if not raw:
        return None
    raw = raw.strip().upper()
    m = re.match(r"^(\d{1,2})-(\d{2})(S?)$", raw)
    if not m:
        return None
    nn, yy, suf = m.groups()
    return f"{int(nn):02d}-{yy}{suf}"


def _classify_event(vrsta_text):
    """Map a Serbian event-type cell to an English report_type, or None."""
    if not vrsta_text:
        return None
    low = vrsta_text.lower()
    for needle, label in _EVENT_MAP:
        if needle in low:
            return label
    return None


def _date_to_iso(cell_text):
    """Parse DD.MM.YYYY (or DD.MM.YY) → ISO 'YYYY-MM-DD', or None."""
    if not cell_text:
        return None
    m = _DATE_RE.search(cell_text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y = 2000 + y if y <= 49 else 1900 + y
    try:
        import datetime
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_listing(index_html):
    """Parse the CINS aviation listing page → list of investigation dicts.

    Each dict has the keys consumed by the pipeline:
        case_id            'NN-YY'  (normalized, canonical)
        report_url         None     (CINS has no separate HTML report page)
        pdf_url            absolute PDF URL (space-encoded)
        pdf_url_es / _en   None     (kept for schema/template parity)
        event_class        'Accident'|'Serious incident'|'Incident'|None
        aircraft           str|None
        registration       str|None ('-' normalized to None)
        date_of_occurrence ISO str|None
        location           str|None
        title              str      (joined row cells, for debugging)

    Only <tr> rows that contain an in-scope aviation PDF href are emitted.
    Annual summaries (godisnji-izvestaji) and any rail/maritime hrefs are
    excluded.  Duplicate case_ids keep the first occurrence.
    """
    rows = []
    seen = set()
    for tr_m in _TR_RE.finditer(index_html):
        tr = tr_m.group(1)
        href_m = _PDF_HREF_RE.search(tr)
        if not href_m:
            continue
        href = href_m.group(1)
        if any(frag in href.lower() for frag in _EXCLUDE_FRAGMENTS):
            continue
        # require an aviation document path
        if "vazdusni-saobracaj" not in href.lower():
            continue

        cells = [_cell_text(c) for c in _TD_RE.findall(tr)]
        if not cells:
            continue

        # anchor text from the link cell (first cell holding the pdf link)
        anchor_m = re.search(
            r"<a[^>]*\.pdf[^>]*>(.*?)</a>", tr, re.IGNORECASE | re.DOTALL
        )
        anchor_text = _cell_text(anchor_m.group(1)) if anchor_m else ""
        link_cell_text = cells[0] if cells else ""

        raw_id = make_case_id(anchor_text, link_cell_text, href)
        case_id = _normalize_case_id(raw_id)
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)

        # Columns: 0=link 1=aircraft 2=vrsta 3=reg 4=date 5=location 6=report-type
        def col(i):
            return cells[i] if i < len(cells) else None

        aircraft = col(1) or None
        event_class = _classify_event(col(2))
        reg = col(3)
        if reg in (None, "", "-", "—", "–"):
            reg = None
        date_iso = _date_to_iso(col(4))
        location = col(5) or None

        pdf_url = _abs_url(href)
        title = " | ".join(c for c in cells if c)

        rows.append({
            "case_id": case_id,
            "report_url": None,
            "pdf_url": pdf_url,
            "pdf_url_es": None,
            "pdf_url_en": None,
            "event_class": event_class,
            "aircraft": aircraft,
            "registration": reg,
            "date_of_occurrence": date_iso,
            "location": location,
            "title": title,
        })
    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url, dest):
    """GET a (already-absolute) PDF URL, URL-encoding any literal spaces.

    The hrefs frequently contain spaces; httpx will reject a raw space in a
    URL, so we percent-encode the path portion while leaving an already-safe
    URL unchanged.  Raises on non-2xx.
    """
    import urllib.parse
    parts = urllib.parse.urlsplit(pdf_url)
    safe_path = urllib.parse.quote(parts.path)
    safe_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )
    resp = client.get(safe_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
