# nsia_ingest/nsia.py
"""
NSIA Norway (Norwegian Safety Investigation Authority, ex-AIBN/SHT) aviation
report listing and detail parser — nsia.no.

Listing: server-rendered paginated table at
    /Aviation/Aviation/Published-reports?page=0..N   (~30 rows/page, ~390
    reports back to 1950; ⚠️ the doubled /Aviation/Aviation/ path is real)
Columns: Report (2024/02) | Aircraft type | Registration | Occurrence date
(DD.MM.YYYY) | Location | Lang. | Safety recommendation.

⚠️ Reports are BILINGUAL per-row: Lang. column says Norwegian or English
(page 0 split ~17 EN / 13 NO).  Norwegian rows get a NO→EN single-pass
rewrite at Phase 3 (the BFU/BEA precedent); we ingest the native PDF text
either way and store lang.

Detail page /Published-reports/{YYYY-NN}: <td>Label</td><td>Value</td>
table (Operator, Type of occurrence, ICAO location, …).  PDF is at the
CONSTRUCTABLE URL {detail}?pid=SHT-Report-ReportFile&attach=1 (clean text
layer on modern reports; 1950s-70s may be scans → tier 'scanned').

case_id: '2024/02' canonicalized to '2024-02'.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://nsia.no"
LISTING_URL = BASE + "/Aviation/Aviation/Published-reports"
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,no;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_DETAIL_HREF_RE = re.compile(r'href="(/Aviation/Aviation/Published-reports/([\d-]+))"')
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _iso_date(s):
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return None


def canonical_case_id(raw):
    """'2024/02' → '2024-02' (slashes/spaces → dash)."""
    return re.sub(r"[^0-9]+", "-", (raw or "").strip()).strip("-") or None


def parse_listing(html):
    """
    One listing page → list of report dicts:
        case_id (canonical '2024-02'), detail_url (absolute), aircraft,
        registration, event_date (ISO), location, lang.
    Rows without a detail link skipped; duplicates dropped.
    """
    out = []
    seen = set()
    for tr in _TR_RE.finditer(html):
        row = tr.group(1)
        href_m = _DETAIL_HREF_RE.search(row)
        if not href_m:
            continue
        tds = [_strip(td) for td in _TD_RE.findall(row)]
        if len(tds) < 6:
            continue
        case_id = canonical_case_id(tds[0])
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        out.append(
            {
                "case_id": case_id,
                "detail_url": urljoin(BASE, href_m.group(1)),
                "aircraft": tds[1] or None,
                "registration": tds[2] or None,
                "event_date": _iso_date(tds[3]),
                "location": tds[4] or None,
                "lang": tds[5] or None,
            }
        )
    return out


# detail page: <td>Label</td><td>Value</td> rows
_DETAIL_FIELDS = {
    "operator": "Operator",
    "report_kind": "Type of occurrence",
}


def parse_detail(html):
    """
    Detail page → dict: operator, report_kind (Accident/Serious incident/…),
    title.  Best-effort; None when absent.
    """
    out = {"operator": None, "report_kind": None, "title": None}
    t = re.search(r"<title>([^<]*)</title>", html or "")
    if t:
        out["title"] = _strip(t.group(1)).removesuffix("| NSIA").strip()
    pairs = {}
    for tr in _TR_RE.finditer(html or ""):
        tds = [_strip(td) for td in _TD_RE.findall(tr.group(1))]
        if len(tds) == 2 and tds[0]:
            pairs.setdefault(tds[0], tds[1])
    for key, label in _DETAIL_FIELDS.items():
        out[key] = pairs.get(label) or None
    return out


def pdf_url(detail_url):
    return f"{detail_url}?pid=SHT-Report-ReportFile&attach=1"


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_page(client, page):
    resp = client.get(LISTING_URL, params={"page": page})
    resp.raise_for_status()
    return resp.text


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
