# uzpln_ingest/uzpln.py
"""
UZPLN — Ústav pro odborné zjišťování příčin leteckých nehod (Czech Republic
aviation accident & serious-incident reports), https://uzpln.gov.cz/.

The catalogue is a SINGLE paginated, server-rendered listing:
    https://uzpln.gov.cz/zpravy-ln?page=N      (10 rows/page, ~68 data pages)
spanning 2003-2025. Plain httpx + a browser UA → HTTP 200; no anti-bot.

⚠️ STOP SIGNAL: pages past the end do NOT 404 — they return a constant
~17.4KB chrome page that carries ZERO `/incident/{id}` links. The discover
walk therefore stops on the FIRST page with no incident links, not on a 404.

Each listing row is a <tr> with cells:
    Vydavatel | Datum události (ISO 'YYYY-MM-DD') | Číslo zprávy (CZ-YY-NNNN,
    may be BLANK for old rows) | Druh zprávy ('Závěrečná zpráva' = final) |
    Místo události (location, often an ICAO like LKNM) | Druh provozu
    (operation type) | Druh události (event type — 'Letecká nehoda' = accident,
    'Vážný incident' = serious incident)
…and a trailing detail link  <a href="/incident/{numeric_id}">.

The detail page  https://uzpln.gov.cz/incident/{id}  is a metadata cover
sheet (NOT the narrative). It repeats the listing fields as a
<th><b>Label:</b></th> <td>value</td> table and ADDS:
    Typ letadla / SLZ  (aircraft type — 'MAGIC M', …)
…and the narrative PDF link  <a href="/pdf/{filename}">.

⚠️ Two PDF filename eras:
  * recent — human-readable WITH SPACES + Czech diacritics, e.g.
    '/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf'  → MUST be URL-encoded.
  * older — random hash, e.g. '/pdf/ecrSLXV8.pdf'.
Both eras carry a real text layer; pdftotext extracts cleanly (Czech).

case_id = Číslo zprávy 'CZ-YY-NNNN' when present (stable, unique). When the
report number is absent (old reports) or would collide, fall back to the
numeric /incident/{id} surrogate ('uzpln-{id}'). The numeric incident_id is
ALWAYS stored as its own column regardless.

⚠️ Registration is NOT on the listing/detail pages; 'OK-[A-Z0-9]{2,4}' is
extracted best-effort from the PDF text (None for foreign-registered).
"""
import html as _html
import re
from urllib.parse import quote

BASE = "https://uzpln.gov.cz"
LISTING = BASE + "/zpravy-ln?page={page}"
DELAY = 1.0
MAX_PAGES = 200  # hard ceiling so a parse regression can't loop forever
# Stop the walk only after this many CONSECUTIVE (confirmed) link-less pages,
# so a single transient blank response (server hiccup) doesn't truncate
# discovery.
EMPTY_STREAK_STOP = 3
# A link-less page is re-fetched up to BLANK_RETRIES times (with a longer pause)
# before being trusted as a real stop page; a rate-limit blank recovers on
# retry, the true end-of-catalogue page stays blank.
BLANK_RETRIES = 2
BLANK_RETRY_BACKOFF = 4  # retry pause = DELAY * this

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "cs,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_TR_RE = re.compile(r"(?is)<tr[>\s].*?</tr>")
_TD_RE = re.compile(r"(?is)<td[^>]*>(.*?)</td>")
_INCIDENT_RE = re.compile(r"/incident/(\d+)")
_PDF_HREF_RE = re.compile(r'href="(/pdf/[^"]+)"', re.IGNORECASE)
_CASE_RE = re.compile(r"\bCZ-\d{2}-\d+\b", re.IGNORECASE)
# Czech civil registration inside the PDF text layer: OK-XXX.
_REG_RE = re.compile(r"\bOK-[A-Z0-9]{2,4}\b")
# ISO date (listing) and dotted date (detail).
_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DOT_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_event_date(text):
    """
    'YYYY-MM-DD' (listing) or 'YYYY.MM.DD' (detail) → ISO. None if unparseable.
    """
    if not text:
        return None
    m = _ISO_RE.search(text) or _DOT_RE.search(text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def list_url(page):
    return LISTING.format(page=page)


def parse_listing(list_html):
    """
    Parse one listing page → list of row dicts in document order. Each:
        incident_id, report_number (case number|None), event_date (ISO|None),
        report_kind, location, operation, event_kind, detail_url.

    A page with NO /incident/ links yields [] — the discover STOP SIGNAL.
    Header rows (no /incident/ link, no <td>) are skipped naturally.
    """
    out = []
    for tr in _TR_RE.findall(list_html or ""):
        mi = _INCIDENT_RE.search(tr)
        if not mi:
            continue  # header row / chrome
        incident_id = mi.group(1)
        tds = _TD_RE.findall(tr)
        if len(tds) < 7:
            continue
        vals = [_strip(t) for t in tds]
        report_number = vals[2] or None
        out.append({
            "incident_id": incident_id,
            "event_date": parse_event_date(vals[1]),
            "report_number": report_number,
            "report_kind": vals[3] or None,
            "location": vals[4] or None,
            "operation": vals[5] or None,
            "event_kind": vals[6] or None,
            "detail_url": f"{BASE}/incident/{incident_id}",
        })
    return out


def has_incident_links(list_html):
    """True iff the page carries at least one /incident/ link (not the stop page)."""
    return bool(_INCIDENT_RE.search(list_html or ""))


# Detail-page key/value rows: <th><b>Label:</b></th> <td>value</td>
_KV_RE = re.compile(
    r"(?is)<th>\s*<b>\s*(.*?)\s*:?\s*</b>\s*</th>\s*<td[^>]*>(.*?)</td>"
)


def _detail_fields(detail_html):
    fields = {}
    for label_raw, val_raw in _KV_RE.findall(detail_html or ""):
        label = _strip(label_raw).rstrip(":").strip()
        fields[label] = _strip(val_raw)
    return fields


def parse_detail(detail_html):
    """
    Parse a /incident/{id} detail page → dict:
        report_number, event_date (ISO|None), report_kind, location,
        operation, event_kind, aircraft, pdf_href (RAW, NOT yet encoded|None).

    Values absent on the page surface as None (e.g. blank Číslo zprávy on old
    reports). pdf_href keeps its raw form (spaces/diacritics) — encode_pdf_href
    turns it into a fetchable URL.
    """
    f = _detail_fields(detail_html)

    def g(label):
        v = f.get(label)
        return v if v else None

    pdf_m = _PDF_HREF_RE.search(detail_html or "")
    pdf_href = _html.unescape(pdf_m.group(1)) if pdf_m else None

    report_number = g("Číslo zprávy")
    if report_number and not _CASE_RE.search(report_number):
        # Defensive: if the cell holds junk, drop it (fall back to surrogate).
        report_number = report_number if report_number.strip() else None

    return {
        "report_number": report_number,
        "event_date": parse_event_date(g("Datum události")),
        "report_kind": g("Druh zprávy"),
        "location": g("Místo události"),
        "operation": g("Druh provozu"),
        "event_kind": g("Druh události"),
        "aircraft": g("Typ letadla / SLZ"),
        "pdf_href": pdf_href,
    }


def encode_pdf_href(href):
    """
    Turn a raw '/pdf/…' href into an absolute, fetchable URL.

    ⚠️ Recent filenames contain SPACES and Czech diacritics
    ('/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf') and MUST be
    percent-encoded; hash filenames ('/pdf/ecrSLXV8.pdf') pass through
    unchanged. '/' is preserved as a path separator.
    """
    if not href:
        return None
    if href.startswith("http"):
        return href
    return BASE + quote(href, safe="/")


def make_case_id(report_number, incident_id, taken=None):
    """
    case_id = the report number (CZ-YY-NNNN, upper-cased) when present and
    not already taken; otherwise the numeric surrogate 'uzpln-{incident_id}'.
    Collision suffix '-2', '-3', … guarantees uniqueness within `taken`.
    """
    if report_number and report_number.strip():
        base = report_number.strip().upper()
    else:
        base = f"uzpln-{incident_id}"
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def extract_registration(text):
    """Best-effort OK- registration from PDF text; None when absent (foreign)."""
    m = _REG_RE.search(text or "")
    return m.group(0).upper() if m else None


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
