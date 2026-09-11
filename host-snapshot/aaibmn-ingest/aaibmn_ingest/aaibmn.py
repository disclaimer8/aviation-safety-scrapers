"""Mongolia AAIB (aaib.gov.mn) HTML scraper.

Source: https://aaib.gov.mn  (Aviation Accident Investigation Bureau, Mongolia)
- Bilingual MN/EN. The EN report category lists final reports in a server-rendered
  <table>: each <tr> has [index, title, report-date, View/Download PDF link].
- The title carries the event date (parenthesised or trailing), aircraft type,
  registration (JU- Mongolian, or foreign EI-CXV / RA-2099G) and event class.
- PDFs live under /uploads/old/<hash>.pdf and are SCANNED IMAGES (no text layer);
  pdftotext yields nothing, so the listing title is the narrative fallback.
- Listing is paginated: /en/c/report?page=1 , ?page=2 , ...
- NOTE: the host is reachable from any vantage (no working geo-block observed),
  but the scraper still supports an httpx SOCKS proxy for the DE-route option.

case_id is INTRINSIC (registration + event-date, else title slug); never an
encounter-order suffix.
"""
import html as _html
import re
from pathlib import Path

from . import text

BASE = "https://aaib.gov.mn"
# EN report category listing (final/serious-incident reports)
LISTING_URL = BASE + "/en/c/report"
REFERER = LISTING_URL
DELAY = 1.8
MAX_PAGES = 25  # safety ceiling for pagination walk

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)


def make_client(proxy=None):
    """Return an httpx.Client with browser UA, optional SOCKS/HTTP proxy."""
    import httpx
    transport = None
    if proxy:
        transport = httpx.HTTPTransport(proxy=proxy)
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
        transport=transport,
    )


def _normalize_case_id(s):
    """Lowercase, collapse non-alnum to single '-', strip edges."""
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def make_case_id(registration, event_date, title):
    """Intrinsic case_id.

    Prefer registration + event-date (both intrinsic to the occurrence). If a
    registration is missing, use the title slug + event-date. As a last resort,
    use the title slug alone.  Never an encounter-order suffix.
    """
    reg = _normalize_case_id(registration) if registration else ""
    date = event_date or ""
    if reg and date:
        return f"{reg}-{date}"
    if reg:
        return reg
    slug = _normalize_case_id(text.slugify(title))[:60]
    if slug and date:
        return f"{slug}-{date}"
    return slug or _normalize_case_id(date) or "aaibmn-unknown"


def _cell_text(td_html):
    return text.strip_html(td_html)


def parse_listing(page_html):
    """Parse one EN report-category page → list of report dicts.

    Each dict:
      case_id            str   intrinsic (reg + date, else title slug)
      pdf_url            str   absolute PDF URL
      pdf_url_en         str   (== pdf_url; single-PDF rows)
      pdf_url_es         None
      report_url         None
      title              str   full listing title
      event_class        str|None
      aircraft           None  (not separately split; carried inside title)
      registration       str|None
      date_of_occurrence str|None  ISO
      location           None
      lang               'en'

    Rows without a PDF link are skipped.
    """
    rows = []
    for tr_m in _TR_RE.finditer(page_html):
        tr = tr_m.group(1)
        pdf_m = _PDF_HREF_RE.search(tr)
        if not pdf_m:
            continue
        pdf_url = _html.unescape(pdf_m.group(1))
        if not pdf_url.startswith("http"):
            pdf_url = BASE + ("" if pdf_url.startswith("/") else "/") + pdf_url

        tds = [_cell_text(m.group(1)) for m in _TD_RE.finditer(tr)]
        # title is the longest non-numeric, non-button cell
        candidates = [
            c for c in tds
            if c and not re.fullmatch(r"[\d.\s]+", c)
            and "download" not in c.lower() and "view" not in c.lower()
        ]
        title = max(candidates, key=len) if candidates else ""
        if not title:
            continue

        event_date = text.parse_event_date(title)
        registration = text.parse_registration(title)
        event_class = text.parse_event_class(title)
        case_id = make_case_id(registration, event_date, title)

        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "pdf_url_en": pdf_url,
            "pdf_url_es": None,
            "report_url": None,
            "title": title,
            "event_class": event_class,
            "aircraft": None,
            "registration": registration,
            "date_of_occurrence": event_date,
            "location": None,
            "lang": "en",
        })
    return rows


def iter_listing_pages(client):
    """Yield (page_url, html) for each report-category page until an empty /
    PDF-less page is reached (or MAX_PAGES)."""
    import time
    for page in range(1, MAX_PAGES + 1):
        url = f"{LISTING_URL}?page={page}"
        resp = client.get(url)
        resp.raise_for_status()
        body = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content
        if "uploads" not in body or not _PDF_HREF_RE.search(body):
            break
        yield url, body
        time.sleep(DELAY)


def download(client, pdf_url, dest):
    """GET pdf_url with Referer header; write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
