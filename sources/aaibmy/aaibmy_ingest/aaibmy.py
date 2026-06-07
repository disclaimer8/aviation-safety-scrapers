# aaibmy_ingest/aaibmy.py
"""
AAIB Malaysia (Air Accident Investigation Bureau, mot.gov.my) hub →
year-page → PDF listing and FILENAME-metadata parser.

The catalogue is server-rendered:
    HUB:  /en/aviation/reports/statistics-and-accident-report-aaib
          → year child pages.  ⚠️ Old years carry a literal 'd' suffix in
          the href (2014d..2021d), recent years do not (2022..2026).
          ALWAYS enumerate from the hub's actual hrefs; never construct.
    YEAR: a server-rendered page with ~8-10 PDF links each.
          PDF hrefs live under
            /en/AAIB Statistic  Accident Report Document/{YEAR}/…
          with literal spaces (sometimes DOUBLE), parens, leading '1. ',
          'updated', trailing '_'.  In the served HTML these are already
          percent-encoded (%20 / %20%20).  We unquote → quote to normalise
          (idempotent) for download.

⚠️ BILINGUAL TRAP: some hrefs point to the Malay copy under /my/AAIBmy…/.
   We keep ONLY /en/ path PDFs and dedupe by report number so an EN+MY
   pair never both ingest.

PDFs are native ENGLISH with clean text layers (14K-28K chars).

⚠️ ALL metadata comes from the FILENAME (there are no per-report detail
   pages); formats drift across eras (number-keyed modern, date-keyed
   legacy).  Any field may be None.
"""
import html as _html
import re
from urllib.parse import quote, unquote, urljoin

BASE = "https://www.mot.gov.my"
HUB_PATH = "/en/aviation/reports/statistics-and-accident-report-aaib"
HUB_URL = BASE + HUB_PATH
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-MY,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Year child pages off the hub: …/statistics-and-accident-report-aaib/2022
# or …/2014d (literal 'd' suffix on old years).
_YEAR_HREF_RE = re.compile(
    re.escape(HUB_PATH) + r"/(\d{4}d?)\b",
)
# PDF links on a year page (both /en/ and /my/ are emitted; filter later).
_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)

# Report number embedded in the filename:
#   "8. A 0822P 9M-SSW Final Report"  → A 08/22P
#   "SI 0124 9M-ITX Final Report"     → SI 01/24
#   "Final Report SI 04-24 9M-LCM"    → SI 04/24
_NUM_RE = re.compile(r"\b(A|SI)[\s_-]?(\d{2})[\s_/-]?(\d{2})(P?)\b")
# Registration in the filename — 9M-XXX / PK-XXX / N### / HS-XXX / I-XXXX.
# No trailing \b after letters (underscores follow, e.g. 9M-MXQ_); use a
# negative letter-lookahead instead.
_REG_RE = re.compile(
    r"\b(9M-[A-Z]{3}|PK-[A-Z]{3}|N\d{2,5}[A-Z]{0,2}|HS-[A-Z]{3}|I-[A-Z]{4})"
    r"(?![A-Za-z])"
)
_NONSLUG = re.compile(r"[^a-z0-9]+")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def year_links(hub_html):
    """
    Distinct year child-page URLs from the hub, IN DOCUMENT ORDER.
    Keeps the literal 'd' suffix on old years (2014d..2021d).
    """
    seen = []
    for m in _YEAR_HREF_RE.finditer(hub_html or ""):
        href = HUB_PATH + "/" + m.group(1)
        url = urljoin(BASE, href)
        if url not in seen:
            seen.append(url)
    return seen


def _normalise_pdf_url(href):
    """
    href verbatim → absolute, percent-encoded URL.  Hrefs in the served
    HTML are already encoded; unquote-then-quote is idempotent and also
    rescues any rare literal-space href.
    """
    href = _html.unescape(href)
    decoded = unquote(href)
    encoded = quote(decoded, safe=":/%?&=#()'-_.~,")
    return urljoin(BASE, encoded)


def pdf_links(year_html):
    """
    EN-only PDF URLs on a year page, deduped by report number so the Malay
    (/my/) copy never co-ingests with its /en/ twin.  Returns a list of
    (pdf_url, filename) in document order.  Filename is the decoded
    basename (no extension) for metadata parsing.
    """
    out = []
    seen_case = set()
    seen_url = set()
    for m in _PDF_HREF_RE.finditer(year_html or ""):
        raw = _html.unescape(m.group(1))
        decoded = unquote(raw)
        # Keep ONLY the English document tree.
        if "/my/" in decoded.lower() or "/aaibmy" in decoded.lower():
            continue
        if "/en/" not in decoded.lower():
            continue
        url = _normalise_pdf_url(raw)
        if url in seen_url:
            continue
        filename = decoded.rsplit("/", 1)[-1]
        if filename.lower().endswith(".pdf"):
            filename = filename[:-4]
        filename = filename.strip()
        # Dedupe by report number (drops EN duplicates / would drop an MY
        # twin too, but those are filtered above already).
        num = _parse_number(filename)
        if num and num in seen_case:
            continue
        if num:
            seen_case.add(num)
        seen_url.add(url)
        out.append((url, filename))
    return out


def _parse_number(filename):
    """Canonical report number from a filename, e.g. 'a-08-22p' / 'si-01-24'."""
    m = _NUM_RE.search(filename or "")
    if not m:
        return None
    occ, yy, nn, p = m.group(1), m.group(2), m.group(3), m.group(4)
    return f"{occ.lower()}-{yy}-{nn}{p.lower()}"


def _slug_fallback(filename):
    base = _NONSLUG.sub("-", (filename or "").lower()).strip("-")
    return base[:40].strip("-") or "report"


def parse_filename(filename):
    """
    Best-effort metadata from a PDF filename →
        case_id (or None — caller supplies the slug fallback),
        registration, report_kind, occurrence_type.  Any field may be None.
    """
    fn = filename or ""
    out = {
        "case_id": _parse_number(fn),
        "registration": None,
        "report_kind": None,
        "occurrence_type": None,
    }

    m = _REG_RE.search(fn)
    if m:
        out["registration"] = m.group(1).upper()

    low = fn.lower()
    if "preliminary" in low:
        out["report_kind"] = "Preliminary"
    elif "interim" in low:
        out["report_kind"] = "Interim"
    elif "final" in low:
        out["report_kind"] = "Final"

    # Occurrence type comes from the report-number prefix: A→Accident,
    # SI→Serious Incident.
    nm = _NUM_RE.search(fn)
    if nm:
        out["occurrence_type"] = (
            "Accident" if nm.group(1).upper() == "A" else "Serious Incident"
        )
    return out


def make_case_id(parsed_num, filename, taken=None):
    """
    Report number when present, else slugified filename[:40].  Collision
    suffix '-2', '-3', … guarantees uniqueness within `taken`.
    """
    base = parsed_num or _slug_fallback(filename)
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_hub(client):
    resp = client.get(HUB_URL)
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
