# aibdk_ingest/aibdk.py
"""
AIB Denmark (Havarikommissionen, en.havarikommissionen.dk) aviation
investigation parser.

Enumeration: the search widget is JS (Dynamicweb AJAX) and the sitemap is
incomplete — BUT any year page (/investigation-results/search-aviation/
{YYYY}) embeds the FULL all-years case list as filter checkboxes:
    <label for="{guid}">0510-2012-102</label>
→ one GET leaks every case_id (~428, 2012-2025; the 0510- prefix is the
aviation-mode code, stripped).

Detail URL: /investigation-results/search-aviation/{FILING_YEAR}/{case_id}.
⚠️ FILING year usually equals the case-id year but NOT always (e.g.
/2015/2018-401) — resolve_detail() tries case-year, ±1, then sweeps
2002..2027.  Misses stay 'new' for the weekly retry.

Case page: <title>Accident to OY-NMX in Kalundborg (EKKL) on 8-10-2023</title>
(registration, ICAO location, date D-M-YYYY) + a CDN PDF link whose text is
"{case_id} (Danish)".  PDFs are DANISH (DA→EN at Phase 3, the BEA/BFU
pattern) and can be HUGE (34MB) — generous timeout.
"""
import html as _html
import re

BASE = "https://en.havarikommissionen.dk"
YEAR_URL = BASE + "/investigation-results/search-aviation/{year}"
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,da;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_LABEL_RE = re.compile(r"<label[^>]*>\s*(?:0510-)?(20\d{2}-\d{3})\s*</label>")
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_PDF_RE = re.compile(
    r'href="(https://cdn\.havarikommissionen\.dk/[^"]+\.pdf)"', re.IGNORECASE
)
_REG_RE = re.compile(r"\b(OY-[A-Z0-9]{3,4}|[A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{2,5}[A-Z]{0,2})\b")
_ICAO_RE = re.compile(r"\(([A-Z]{4})\)")
_DATE_RE = re.compile(r"\bon\s+(\d{1,2})-(\d{1,2})-(\d{4})")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_case_ids(html):
    """Year-page HTML → sorted unique case_ids (the checkbox leak)."""
    return sorted(set(_LABEL_RE.findall(html or "")))


def candidate_years(case_id):
    """Filing-year candidates: case-year, ±1, then a 2002..2027 sweep."""
    y = int(case_id[:4])
    tried = [y, y + 1, y - 1]
    sweep = [x for x in range(2002, 2028) if x not in tried]
    return [str(x) for x in tried + sweep]


def detail_url(year, case_id):
    return f"{BASE}/investigation-results/search-aviation/{year}/{case_id}"


def parse_case(html):
    """
    Case page → dict: title, registration, location (incl. ICAO),
    event_date (from 'on D-M-YYYY'), pdf_url (CDN, or None).
    """
    out = {"title": None, "registration": None, "location": None,
           "event_date": None, "pdf_url": None}
    t = _TITLE_RE.search(html or "")
    if t:
        title = _strip(t.group(1)).split("|")[0].strip()
        out["title"] = title or None
        reg = _REG_RE.search(title or "")
        if reg:
            out["registration"] = reg.group(1)
        d = _DATE_RE.search(title or "")
        if d:
            day, mo, y = d.groups()
            try:
                out["event_date"] = f"{int(y):04d}-{int(mo):02d}-{int(day):02d}"
            except ValueError:
                pass
        # location: between 'in/at/near' and ' on '
        loc = re.search(r"\b(?:in|at|near)\s+(.{3,60}?)\s+on\s+\d", title or "")
        if loc:
            out["location"] = loc.group(1).strip(" ,")

    for href in _PDF_RE.findall(html or ""):
        out["pdf_url"] = _html.unescape(href)
        break
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_year_page(client, year):
    resp = client.get(YEAR_URL.format(year=year))
    resp.raise_for_status()
    return resp.text


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def resolve_detail(client, case_id, max_tries=28):
    """Try filing-year candidates until the case page resolves (200 w/ title)."""
    import time

    for year in candidate_years(case_id)[:max_tries]:
        time.sleep(DELAY)
        try:
            resp = client.get(detail_url(year, case_id))
            if resp.status_code == 200 and case_id.split("-")[0] in resp.text:
                return detail_url(year, case_id), resp.text
        except Exception:
            continue
    return None, None


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
