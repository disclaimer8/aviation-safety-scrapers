# sacaa_ingest/sacaa.py
"""
SACAA AIID (South African Civil Aviation Authority, Accident & Incident
Investigation Division) listing parser — caa.co.za.

TWO listing pages, each embedding the ENTIRE dataset as static server-
rendered <table> HTML (the DataTables JS is client-side cosmetics only):
  - /industry-information/accidents-and-incidents/accidents-and-incident-reports/
      table A "latest" (4 cols: Title | Registration | Date | File)
      table B main    (7 cols: Year | Date | Type | Location | Name | Reg | File)
  - /industry-information/accidents-and-incidents/accidents-and-incidents-archive/
      two 7-col tables (2008-2017 and 1953-2007 eras)

Row semantics:
  - Year column is a numeric year OR a category: 'Preliminary Reports',
    'Interim Reports', 'Foreign Reports', 'PASA Reports'. Category rows
    carry the FULL date (with year) in the Date column; numeric-year rows
    have day+month only ("2 March") — the year comes from the Year column.
  - Name column = numeric AIID report id (tail of ref CA18/2/3/{id}) for
    final reports, or a registration / free text for category rows.
  - File href = public Azure Blob PDF (4 era containers; hrefs may contain
    spaces — take verbatim and percent-encode).

English throughout; metadata is complete in the listing (no PDF metadata
parse needed). Some archive-era and prior-2010 PDFs are scans (no text
layer) → pipeline marks source_tier='scanned'.
"""
import html as _html
import re
from urllib.parse import quote

MAIN_URL = ("https://www.caa.co.za/industry-information/"
            "accidents-and-incidents/accidents-and-incident-reports/")
ARCHIVE_URL = ("https://www.caa.co.za/industry-information/"
               "accidents-and-incidents/accidents-and-incidents-archive/")
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)

_CATEGORIES = {
    "preliminary reports": "Preliminary",
    "interim reports": "Interim",
    "foreign reports": "Foreign",
    "pasa reports": "PASA",
}

_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}
_MONTHS.update({m[:3]: v for m, v in list(_MONTHS.items())})

# "2 March" / "27 January 2026" / "17 February"
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(text, fallback_year=None):
    """'2 March' + year-from-column, or '27 January 2026' → ISO, else None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    d, mon, y = m.groups()
    mo = _MONTHS.get(mon.lower()[:3]) or _MONTHS.get(mon.lower())
    y = y or fallback_year
    if not mo or not y:
        return None
    try:
        return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
    except ValueError:
        return None


def _encode_url(href):
    """Percent-encode the path of a blob href (filenames contain spaces)."""
    return quote(_html.unescape(href), safe=":/%()&,'-_.~")


def parse_listing(html):
    """
    Parse one listing page → list of report dicts (ALL tables on the page):
        pdf_url      – percent-encoded absolute blob URL
        name         – Name/Title cell verbatim (numeric id or reg/text)
        report_kind  – 'Final' | 'Preliminary' | 'Interim' | 'Foreign' | 'PASA'
        aircraft     – Aircraft Type cell or None (latest-table rows lack it)
        registration – Registration cell or None
        location     – Location cell or None
        event_date   – ISO date or None
    Rows without a .pdf href are skipped. Order preserved; duplicate
    pdf_urls dropped (a report can appear on both pages).
    """
    out = []
    seen = set()
    for t in _TABLE_RE.findall(html):
        for tr in _TR_RE.finditer(t):
            row_html = tr.group(1)
            href_m = _HREF_RE.search(row_html)
            if not href_m:
                continue
            pdf_url = _encode_url(href_m.group(1))
            if pdf_url in seen:
                continue
            tds = [_strip(td) for td in _TD_RE.findall(row_html)]
            if len(tds) >= 7:
                year, date_s, aircraft, location, name, reg = tds[:6]
            elif len(tds) >= 4:
                # latest table: Title | Registration | Date | File
                name, reg, date_s = tds[0], tds[1], tds[2]
                year, aircraft, location = "", None, None
            else:
                continue

            kind = _CATEGORIES.get(year.lower())
            if kind is None and "preliminary" in name.lower():
                kind = "Preliminary"
            fallback_year = year if year.isdigit() else None
            event_date = _iso_date(date_s, fallback_year)

            seen.add(pdf_url)
            out.append(
                {
                    "pdf_url": pdf_url,
                    "name": name or None,
                    "report_kind": kind or "Final",
                    "aircraft": (aircraft or None),
                    "registration": (reg or None),
                    "location": (location or None),
                    "event_date": event_date,
                }
            )
    return out


def make_case_id(name, registration, event_date, taken=None):
    """
    case_id: the numeric AIID id when Name is numeric (tail of CA18/2/3/{id});
    else slug of registration+date (category rows); else slug of name.
    Collision suffix _2, _3…
    """
    from .text import slugify

    name = (name or "").strip()
    if name.isdigit():
        base = name
    elif registration and event_date:
        base = f"{slugify(registration)}-{event_date}"
    elif name:
        base = slugify(name)[:60]
    else:
        base = "report"
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}_{n}"
        n += 1
    return cand


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
