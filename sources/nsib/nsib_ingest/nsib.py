# nsib_ingest/nsib.py
"""NSIB (Nigeria Safety Investigation Bureau) air-reports scraper.

Source: https://nsib.gov.ng/air-reports/
- A single paginated, server-rendered HTML TABLE lists every air report row.
  Each <tr> carries all metadata in <td> cells plus a direct download link to
  the report PDF (when one exists).  No ninja-forms gate is required for the
  download — the PDF href is a plain wp-content/uploads URL.
- Pagination is via ?nsibcrv_category_view_<id>_page=N (1..N).  We follow the
  numbered page links discovered on page 1.
- Report bodies are real text PDFs (preliminary reports ~13pp, interim
  statements ~3-4pp); pdftotext extracts them directly (no OCR needed).
- The ~86 "Final Report" rows carry NO downloadable PDF (boilerplate post page
  only) and are skipped at build time (no narrative).

Yield (2026-06): 286 air-report rows, 33 with a downloadable PDF
  (14 preliminary, 18 interim, 1 misc).  0 of 86 finals downloadable.
"""
import html as _html
import re
import sys as _sys
import time

BASE = "https://nsib.gov.ng"
INDEX_URL = BASE + "/air-reports/"
# JSON API. Returns every report the site holds, including many absent from the
# paginated HTML table (295 total / 292 aviation vs 117 via the table, measured
# 2026-08-03). Page size is server-capped at 200 regardless of ?limit.
API_URL = BASE + "/api/reports"
API_PAGE = 200
REFERER = INDEX_URL
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# Numbered pagination links: ?nsibcrv_category_view_<id>_page=N
_PAGE_LINK_RE = re.compile(
    r'href="(/air-reports/\?nsibcrv_category_view_\d+_page=(\d+))"'
)

# A data row carries the status span; non-data <tr> (header) does not.
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
_TD_RE = re.compile(r"<td>(.*?)</td>", re.DOTALL)
_HREF_RE = re.compile(r'href="([^"]+)"')
_PDF_HREF_RE = re.compile(
    r'href="(https://nsib\.gov\.ng/wp-content/uploads/[^"]+?\.pdf)', re.I
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def date_from_report_no(report_no):
    """Occurrence date out of an NSIB report number, or None.

    Report numbers embed the occurrence date as .../YYYY/MM/DD/... — e.g.
    "NCAT/2025/07/23/INTR/01" is an event of 2025-07-23 (published a year
    later). This is the ONLY occurrence date the API exposes; published_at is
    the publication date and must never be used as one.

    A year-only number yields None rather than YYYY-01-01: a fabricated day is
    worse than a missing date, because the occurrences loader honestly drops a
    dateless row while a fake one propagates.
    """
    if not report_no:
        return None
    m = re.search(r"(?:^|/)(19\d{2}|20\d{2})/([01]?\d)/([0-3]?\d)(?:/|$)", str(report_no))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def fetch_api_rows(client):
    """Enumerate the JSON API and return rows shaped like parse_listing().

    Same dict keys, so both discovery paths feed the identical insert logic in
    pipeline.discover — no second code path to drift.

    Returns [] on any failure: the API is a supplement, and an outage must not
    take down discovery from the HTML table.
    """
    rows = []
    page = 1
    seen = set()
    while True:
        try:
            resp = client.get(f"{API_URL}?limit={API_PAGE}&page={page}")
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - supplement, never fatal
            print(f"[nsib api] page {page}: {exc}", file=_sys.stderr)
            break
        batch = payload.get("reports") or []
        if not batch:
            break
        for rec in batch:
            # The API also carries railway and maritime reports (3 of 295).
            if (rec.get("sector") or "").lower() != "aviation":
                continue
            pdf_url = rec.get("file_url")
            rid = rec.get("id")
            if not pdf_url or rid in seen:
                continue
            # The API gives a site-relative path ("/uploads/reports/x.pdf")
            # where the HTML listing gives an absolute URL. Downstream fetch
            # hands the value straight to httpx, which rejects a bare path.
            if not pdf_url.startswith(("http://", "https://")):
                pdf_url = BASE + "/" + pdf_url.lstrip("/")
            seen.add(rid)
            rows.append({
                "date": date_from_report_no(rec.get("report_no")),
                "post_url": None,
                "title": rec.get("title"),
                "operator": rec.get("operator"),
                "category": None,
                "event_class": rec.get("occurrence"),
                "report_type_col": None,
                "reg_cell": rec.get("reg_no"),
                "status": rec.get("report_status"),
                "pdf_url": pdf_url,
            })
        total = payload.get("total")
        if total is not None and len(seen) >= int(total):
            break
        if len(batch) < API_PAGE:
            break
        page += 1
        time.sleep(DELAY)
    return rows


def _txt(cell):
    return _WS.sub(" ", _html.unescape(_TAG.sub(" ", cell))).strip()


def make_client():
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


def iter_page_urls(index_html):
    """
    Return [INDEX_URL, page2, page3, ...] — page 1 plus every numbered page
    link found on it, de-duplicated and ordered by page number.
    """
    pages = {1: INDEX_URL}
    for m in _PAGE_LINK_RE.finditer(index_html):
        n = int(m.group(2))
        if n not in pages:
            pages[n] = BASE + _html.unescape(m.group(1))
    return [pages[k] for k in sorted(pages)]


def parse_listing(page_html):
    """
    Parse one air-reports listing page into a list of row dicts.

    Each row dict (only rows whose status cell is present are returned):
      date            ISO 'YYYY-MM-DD' or None
      post_url        the per-report post URL (td[1] anchor)
      title           cleaned title text (td[1])
      operator        td[2]
      category        td[3]
      event_class     td[4]   ('Accident' / 'Serious Incident' / 'Incident')
      report_type_col td[5]   ('Air Accident Report' etc)
      reg_cell        td[6]   raw registration cell (may be garbled)
      status          td[7]   ('Preliminary Report' / 'Interim Statement' / 'Final Report')
      pdf_url         td[8] download href, or None

    The PDF href is trimmed at the first '.pdf' to defeat the site's
    doubled-filename artifact (…18.pdfPreliminary-Report-18.pdf).
    """
    rows = []
    for rm in _ROW_RE.finditer(page_html):
        body = rm.group(1)
        if "nsib-crv-status" not in body:
            continue  # header / non-data row
        tds = _TD_RE.findall(body)
        if len(tds) < 8:
            continue

        post_m = _HREF_RE.search(tds[1])
        post_url = _html.unescape(post_m.group(1)) if post_m else None

        pdf_url = None
        if len(tds) >= 9:
            pm = _PDF_HREF_RE.search(tds[8])
            if pm:
                pdf_url = pm.group(1)

        date = _txt(tds[0]) or None
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = None

        rows.append({
            "date": date,
            "post_url": post_url,
            "title": _txt(tds[1]) or None,
            "operator": _txt(tds[2]) if len(tds) > 2 else None,
            "category": _txt(tds[3]) if len(tds) > 3 else None,
            "event_class": _txt(tds[4]) if len(tds) > 4 else None,
            "report_type_col": _txt(tds[5]) if len(tds) > 5 else None,
            "reg_cell": _txt(tds[6]) if len(tds) > 6 else None,
            "status": _txt(tds[7]) if len(tds) > 7 else None,
            "pdf_url": pdf_url,
        })
    return rows


def download(client, pdf_url, dest):
    """GET pdf_url with Referer and write bytes to dest. Raises on non-2xx."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
