# aaiu_ingest/aaiu.py
"""
AAIU Ireland (Air Accident Investigation Unit, aaiu.ie) REST listing and
report-page parser.

The whole catalogue is the open WordPress REST API:
    GET /wp-json/wp/v2/aaiu_report?per_page=100&page=1..6  (x-wp-total: 560)
Each row: id, date, slug, link, title.rendered, content.rendered (an
English synopsis, ~1-2K chars).  ⚠️ acf fields are EMPTY — metadata comes
from the TITLE (best-effort; formats drift across eras):
  new:  "Final Report: Accident involving an Airbus A321-271NX (neo),
         registration TC-LTL, at Dublin Airport (EIDW), Ireland on
         18 October 2024. Report 2026-004"
  old:  "ACCIDENT Cessna 172 EI-XXX ..." (CAPS, no report number — 12/560)

The report PDF is linked from the POST PAGE (not the REST content) —
first aaiu.ie/wp-content/uploads/*.pdf href.  A few legacy posts have no
PDF → the synopsis itself is the narrative (tier 'html').
"""
import html as _html
import re

BASE = "https://aaiu.ie"
REST_URL = BASE + "/wp-json/wp/v2/aaiu_report"
PER_PAGE = 100
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

_NUM_RE = re.compile(r"\b(\d{4}-\d{3})\b")
_KIND_RE = re.compile(
    r"^\s*(Final Report|Preliminary Report|Interim Statement|Synoptic Report)",
    re.IGNORECASE,
)
# "registration TC-LTL" / "registrations EI-A and EI-B"
_REG_PHRASE_RE = re.compile(r"registrations?,?\s+([A-Z0-9-]{3,10})", re.IGNORECASE)
# bare reg forms: EI-ABC, G-ABCD, ZK-XXX, N123AB, TC-LTL …
_REG_BARE_RE = re.compile(r"\b([A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{2,5}[A-Z]{0,2})\b")
# "involving a/an <aircraft>, registration" / "involving <aircraft> EI-XXX"
_AIRCRAFT_RE = re.compile(
    r"involving\s+(?:an?\s+)?(.+?)(?:,?\s+registration|,?\s+\b[A-Z]{1,2}-[A-Z0-9]{3,5}\b)",
    re.IGNORECASE,
)
# "at <location> on <date>" / "near <location>, ... on <date>"
_LOC_RE = re.compile(r"\b(?:at|near)\s+(.{3,70}?)\s+on\s+\d{1,2}\s+[A-Z][a-z]+",)
_DATE_RE = re.compile(r"\bon\s+(\d{1,2})\s+([A-Z][a-z]+),?\s+(\d{4})")

_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}

# PDFs live under TWO paths: modern /wp-content/uploads/… and legacy (Drupal
# era) /sites/default/files/report-attachments/… — the latter with literal
# spaces in filenames ("REPORT 2019-003.pdf") → percent-encode.
_PDF_HREF_RE = re.compile(
    r'href="(https://(?:www\.)?aaiu\.ie/(?:wp-content/uploads|sites/default/files)/[^"]+\.pdf)"',
    re.IGNORECASE,
)


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def synopsis_text(content_rendered):
    """content.rendered HTML → plain text (paragraph breaks kept)."""
    text = re.sub(r"<br\s*/?>", "\n", content_rendered or "")
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_title(title):
    """
    Best-effort metadata from a report title →
        case_id (YYYY-NNN or None), report_kind, aircraft, registration,
        location, event_date.  Any field may be None.
    """
    t = _strip(title)
    out = {"case_id": None, "report_kind": None, "aircraft": None,
           "registration": None, "location": None, "event_date": None}

    m = _NUM_RE.search(t)
    if m:
        out["case_id"] = m.group(1)

    m = _KIND_RE.match(t)
    if m:
        k = m.group(1).title()
        out["report_kind"] = "Interim" if "Interim" in k else k.split()[0]
    else:
        out["report_kind"] = "Final"  # legacy CAPS titles are final reports

    m = _REG_PHRASE_RE.search(t)
    if m:
        out["registration"] = m.group(1).upper().rstrip(",.")
    else:
        m = _REG_BARE_RE.search(t)
        if m:
            out["registration"] = m.group(1)

    m = _AIRCRAFT_RE.search(t)
    if m:
        out["aircraft"] = m.group(1).strip(" ,")

    m = _LOC_RE.search(t)
    if m:
        out["location"] = m.group(1).strip(" ,")

    m = _DATE_RE.search(t)
    if m:
        d, mon, y = m.groups()
        mo = _MONTHS.get(mon.lower())
        if mo:
            out["event_date"] = f"{int(y):04d}-{mo:02d}-{int(d):02d}"
    return out


def make_case_id(parsed_num, wp_id, taken=None):
    """AAIU report number YYYY-NNN; legacy posts without one → 'wp-{id}'."""
    base = parsed_num or f"wp-{wp_id}"
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def find_pdf_url(page_html):
    """First aaiu.ie report PDF href on a report page, or None."""
    m = _PDF_HREF_RE.search(page_html or "")
    if not m:
        return None
    from urllib.parse import quote

    return quote(_html.unescape(m.group(1)), safe=":/%()&,'-_.~")


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_page(client, page):
    resp = client.get(REST_URL, params={"per_page": PER_PAGE, "page": page})
    resp.raise_for_status()
    return resp.json()


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
