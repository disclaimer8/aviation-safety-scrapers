# aaiube_ingest/aaiube.py
"""
AAIU Belgium (Air Accident Investigation Unit, FPS Mobility & Transport)
single-page table parser.

The whole catalogue is ONE server-rendered page:
    GET https://mobilit.belgium.be/en/aviation/accidents-and-incidents/
        safety-investigations-and-reports
Plain curl + browser UA → 200 (no anti-bot, clean TLS chain).

The page is an accordion: one <h3>Reports occurrences YYYY</h3> heading per
year, each followed by a <table> with five columns:
    Date of occurrence | Type of aircraft | Casualties | Location | Status
Cells are sometimes bare, sometimes <p>-wrapped; a few rows carry an extra
trailing "safety recommendations" cell (5/6/7 cells). The report PDF lives in
the Status cell as the FIRST <a href="…​.pdf"> in the row. Rows whose status is
plain text ("In progress", "Delegated to NL", …) have no PDF → skipped.

PDFs live under /sites/default/files/documents/publications/{year}/… and are
mostly ENGLISH with clean text layers (a few older FR/NL). A few have
_0 / V1 re-upload suffixes.

case_id derivation:
  modern  filename carries an AAIU ref 'AAIU-2022-09-12-01' → 'aaiu-2022-09-12-01'
          (lowercased verbatim; Ireland uses 'YYYY-NNN', no collision shape)
  legacy  bare/odd filenames (1.pdf, 2009_01.pdf, AA-7-1.pdf) → 'be-{YYYY}-{slug}'
          where YYYY is the occurrence year and slug is the slugified stem.
Collisions get a '-2', '-3' … suffix.
"""
import html as _html
import re
from urllib.parse import quote, urljoin

BASE = "https://mobilit.belgium.be"
LISTING_URL = (
    BASE + "/en/aviation/accidents-and-incidents/"
    "safety-investigations-and-reports"
)
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-BE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# AAIU-2022-09-12-01 anywhere in a filename (modern ref).
_MODERN_REF_RE = re.compile(r"AAIU-(\d{4})-(\d{2})-(\d{2})-(\d{2})", re.IGNORECASE)
# Report-kind keywords found in the Status link text / filename.
_KIND_PATTERNS = [
    ("Final", re.compile(r"\bfinal\b", re.IGNORECASE)),
    ("Preliminary", re.compile(r"\bprelim", re.IGNORECASE)),
    ("Interim", re.compile(r"\binterim\b", re.IGNORECASE)),
    ("Progress", re.compile(r"\bprogress\b", re.IGNORECASE)),
    ("Statement", re.compile(r"\bstatement\b", re.IGNORECASE)),
]
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_YEAR_HEADING_RE = re.compile(
    r"Reports?\s+occurrences?\s+(\d{4})", re.IGNORECASE
)
_NONSLUG_RE = re.compile(r"[^a-z0-9]+")

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_PDF_HREF_RE = re.compile(r'href="([^"]+?\.pdf)"', re.IGNORECASE)


def _cell_text(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def slugify(s):
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def absolutise_pdf_url(href):
    """Make a (possibly relative) PDF href absolute and percent-encode spaces."""
    if not href:
        return None
    url = urljoin(BASE + "/", _html.unescape(href.strip()))
    return quote(url, safe=":/%()&,'-_.~")


def detect_kind(text):
    """Report kind from Status link text / filename; default 'Final'."""
    for kind, rx in _KIND_PATTERNS:
        if rx.search(text or ""):
            return kind
    return "Final"


def detect_lang(filename):
    """Default 'en'; tag 'fr'/'nl' only when the filename clearly says so."""
    f = (filename or "").lower()
    if re.search(r"(?:^|[^a-z])(fr|french|francais|rapport)(?:[^a-z]|$)", f):
        return "fr"
    if re.search(r"(?:^|[^a-z])(nl|dutch|verslag)(?:[^a-z]|$)", f):
        return "nl"
    return "en"


def derive_case_id(pdf_url, year=None, taken=None):
    """
    case_id from the PDF filename.
      modern AAIU-ref  → 'aaiu-2022-09-12-01' (lowercased verbatim)
      legacy           → 'be-{year}-{slugified-stem[:30]}'
    Collisions get a numeric suffix.
    """
    filename = pdf_url.rstrip("/").split("/")[-1]
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = _html.unescape(stem)

    m = _MODERN_REF_RE.search(stem)
    if m:
        base = "aaiu-{}-{}-{}-{}".format(*m.groups()).lower()
    else:
        yr = year or "0000"
        base = f"be-{yr}-{slugify(stem)[:30]}".rstrip("-")

    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def parse_listing(page_html):
    """
    Parse the single listing page → list of report dicts (one per PDF-bearing
    row), each: pdf_url, date_of_occurrence (YYYY-MM-DD|None), aircraft,
    casualties, location, status, report_kind, lang, year.

    Rows without a PDF link are dropped (interim/in-progress/delegated). The
    enclosing <h3>Reports occurrences YYYY</h3> heading supplies the year used
    for legacy case_ids and as a date fallback.
    """
    out = []
    # Walk the page splitting on year headings so each table knows its year.
    headings = list(_YEAR_HEADING_RE.finditer(page_html))
    segments = []  # (year, html_slice)
    for i, h in enumerate(headings):
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(page_html)
        segments.append((h.group(1), page_html[start:end]))
    if not segments:
        segments = [(None, page_html)]

    for year, segment in segments:
        for table in _TABLE_RE.findall(segment):
            for row in _ROW_RE.findall(table):
                if "<th" in row.lower():
                    continue
                pdf_m = _PDF_HREF_RE.search(row)
                if not pdf_m:
                    continue
                pdf_url = absolutise_pdf_url(pdf_m.group(1))
                cells = [_cell_text(c) for c in _CELL_RE.findall(row)]

                def cell(i):
                    return cells[i] if i < len(cells) else ""

                date_raw = cell(0)
                aircraft = cell(1) or None
                casualties = cell(2) or None
                location = cell(3) or None
                # Status link text = the anchor text in the row.
                link_text_m = re.search(
                    r"<a\b[^>]*\.pdf[^>]*>(.*?)</a>", row,
                    re.IGNORECASE | re.DOTALL,
                )
                status = _cell_text(link_text_m.group(1)) if link_text_m else cell(4)

                event_date = None
                dm = _DATE_RE.search(date_raw)
                if dm:
                    d, mo, y = (int(x) for x in dm.groups())
                    event_date = f"{y:04d}-{mo:02d}-{d:02d}"

                filename = pdf_url.split("/")[-1]
                out.append(
                    {
                        "pdf_url": pdf_url,
                        "date_of_occurrence": event_date,
                        "aircraft": aircraft,
                        "casualties": casualties,
                        "location": location,
                        "status": status or None,
                        "report_kind": detect_kind(f"{status} {filename}"),
                        "lang": detect_lang(filename),
                        "year": year,
                    }
                )
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing(client):
    resp = client.get(LISTING_URL)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
