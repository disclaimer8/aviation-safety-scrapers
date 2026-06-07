# araib_ingest/araib.py
"""
ARAIB South Korea (Aviation and Railway Accident Investigation Board,
araib.molit.go.kr) — English aviation accident-report board parser.

⚠️ Access gate (TmaxSoft WebtoB): the first HTTPS GET returns an HTTP/1.0
*307 to the SAME URL* with `Set-Cookie: TMOSHCooKie=...`. The client MUST
replay that cookie; a persistent `httpx.Client` with `follow_redirects=True`
and its built-in cookie jar handles the handshake transparently (subsequent
requests then carry JSESSIONID etc. and return 200). HTTPS ONLY — port 80 is
a connection reset. Cold TLS connections intermittently reset → every fetch is
wrapped in a retry/backoff loop (see fetch_page / download_pdf).

The catalogue is ONE server-rendered JSP board (3-stage source):

    LISTING  https://araib.molit.go.kr/USR/BORD0201/m_34591/LST.jsp?id=eaib0401
        10 rows/page, pagination `&lcmspage=N`. ⚠️ The paginator widget renders
        only a fixed window of page links — DO NOT trust it. Walk pages until a
        page yields no NEW idx, then stop. TOTAL = 55 English aviation reports.

    DETAIL   .../m_34591/DTL.jsp?id=eaib0401&mode=view&idx=NNNNNN
        idx = the stable numeric listing row id. The DTL page carries the full
        (untruncated) title + the PDF download link.

    PDF      https://araib.molit.go.kr/LCMS/DWN.jsp?fold=/eaib0401/&fileName=<enc>.pdf
        ⚠️ fileName is non-uniform (human-readable 'HL8088+Preliminary+...pdf'
        OR url-encoded '%28AIR1906%29_...pdf' OR timestamp-prefixed) — ALWAYS
        scraped from the DTL page, NEVER constructed.

⚠️ `id`↔`m_` node binding is STRICT: `eaib0401` is only valid under `m_34591`.
A wrong pairing returns a ~624-byte '페이지 이동중' redirect stub — any response
under TINY_STUB_BYTES is treated as a fetch failure (see looks_like_stub).

case_id (set at fetch from the PDF synopsis):
    canonical = the ARAIB case number normalised from the synopsis
        ('Accident Number: AAR2404' / 'ARAIB/AAR2203' / 'ARAIB/AIR1906'
         → 'aar2404' / 'air1906').
    fallback  = 'araib-{idx}' when no case number is extractable.
`idx` is stored always and is the report-table primary key.

The English board PDFs have clean text layers across all eras (6-78K chars);
lang = 'en'.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://araib.molit.go.kr"
# ⚠️ strict id↔m_ binding: eaib0401 only valid under m_34591.
LISTING = (
    "https://araib.molit.go.kr/USR/BORD0201/m_34591/LST.jsp?id=eaib0401"
)
DTL_BASE = "https://araib.molit.go.kr/USR/BORD0201/m_34591/DTL.jsp"
DELAY = 1.0
# Cold-TLS reset handling: how many extra attempts + base backoff seconds.
RETRIES = 4
BACKOFF = 2.5
# A genuine board page is tens of KB; the wrong-node redirect stub is ~624 B.
TINY_STUB_BYTES = 2000

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# ── listing parse ───────────────────────────────────────────────────────────
_TR_RE = re.compile(r"(?is)<tr[>\s].*?</tr>")
_TD_RE = re.compile(r"(?is)<td[^>]*>(.*?)</td>")
_DTL_IDX_RE = re.compile(r"DTL\.jsp\?[^\"']*?\bidx=(\d+)", re.IGNORECASE)
# The title cell is <td class="tl"><a ...>TITLE</a></td>; the title in the
# listing is often '...'-truncated (full title comes from the DTL page).
_TITLE_CELL_RE = re.compile(
    r'(?is)class="tl"[^>]*>\s*<a[^>]*>(.*?)</a>'
)
_PUB_DATE_RE = re.compile(r"\b(\d{4})\.(\d{2})\.(\d{2})\b")

# ── DTL parse ───────────────────────────────────────────────────────────────
# The download link on the DTL page: href="/LCMS/DWN.jsp?fold=/eaib0401/&...".
_DWN_HREF_RE = re.compile(
    r'href="([^"]*?/LCMS/DWN\.jsp\?[^"]+)"', re.IGNORECASE
)
# Full title cell on the DTL view table: <th ...>Title</th><td ...>VALUE</td>.
_DTL_TITLE_RE = re.compile(
    r"(?is)>\s*Title\s*</th>\s*<td[^>]*>(.*?)</td>"
)

# ── synopsis (PDF text) extraction ──────────────────────────────────────────
# Case number: 'Accident Number: AAR2404', 'Incident Number: AIR1906', or the
# header form 'ARAIB/AAR2203' / 'ARAIB/AIR1906'. Letter group AAR (accident) or
# AIR (incident), 4 digits.
_CASE_LABEL_RE = re.compile(
    r"(?:Accident|Incident)\s+Number\s*[:：]?\s*(A[AI]R\s*[-/]?\s*\d{3,4})",
    re.IGNORECASE,
)
_CASE_ARAIB_RE = re.compile(r"ARAIB\s*/\s*(A[AI]R\s*[-/]?\s*\d{3,4})",
                            re.IGNORECASE)
# Registration: Korean civil HL prefix, optional dash (HL8088 / HL-7525).
_REG_RE = re.compile(r"\bHL-?\d{4}\b", re.IGNORECASE)

# Occurrence date (NOT the publish date). Variants seen in synopses:
#   'December 29, 2024'                 (Month D, Year)
#   'Date & Time: 29 Oct, 2019'         (D Mon, Year, abbreviated month)
#   'Date of accident: November 27, 2022'
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON_NAME = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)
# 'Month D, Year'  (e.g. 'December 29, 2024')
_DATE_MDY_RE = re.compile(
    rf"{_MON_NAME}\.?\s+(\d{{1,2}})\s*,?\s+(\d{{4}})", re.IGNORECASE
)
# 'D Month, Year'  (e.g. '29 Oct, 2019' / '29 October 2019')
_DATE_DMY_RE = re.compile(
    rf"(\d{{1,2}})\s+{_MON_NAME}\.?\s*,?\s+(\d{{4}})", re.IGNORECASE
)
# Operator: 'Operator: Jeju Air Co., Ltd'
_OPERATOR_RE = re.compile(r"Operator\s*[:：]\s*([^\n\r]+)", re.IGNORECASE)
# Aircraft: 'Aircraft: Boeing 737-800'
_AIRCRAFT_RE = re.compile(r"Aircraft\s*[:：]\s*([^\n\r]+)", re.IGNORECASE)
# Location: 'Location: Muan International Airport (RKJB)'
_LOCATION_RE = re.compile(r"Location\s*[:：]\s*([^\n\r]+)", re.IGNORECASE)


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# ── listing ─────────────────────────────────────────────────────────────────


def listing_page_url(page):
    """Listing URL for page N (1-based) via the &lcmspage=N param."""
    return f"{LISTING}&lcmspage={int(page)}"


def dtl_url(idx):
    """Detail-page URL for a listing row id."""
    return f"{DTL_BASE}?id=eaib0401&mode=view&idx={idx}"


def parse_listing(page_html):
    """
    Parse one listing page → list of row dicts in document order. Each dict:
        idx, dtl_url, title (may be '...'-truncated), publish_date (ISO|None),
        view_count.
    Only <tr>s that carry a DTL.jsp?...idx=N link are data rows; the header row
    and the paginator are skipped (they have no idx link).
    """
    out = []
    seen = set()
    for tr in _TR_RE.findall(page_html or ""):
        m = _DTL_IDX_RE.search(tr)
        if not m:
            continue
        idx = m.group(1)
        if idx in seen:
            continue
        seen.add(idx)

        tm = _TITLE_CELL_RE.search(tr)
        title = _strip(tm.group(1)) if tm else None

        tds = [_strip(td) for td in _TD_RE.findall(tr)]
        view_count = tds[-1] if tds else None

        dm = _PUB_DATE_RE.search(tr)
        publish_date = (
            f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else None
        )

        out.append({
            "idx": idx,
            "dtl_url": dtl_url(idx),
            "title": title,
            "publish_date": publish_date,
            "view_count": view_count,
        })
    return out


# ── DTL ─────────────────────────────────────────────────────────────────────


def looks_like_stub(text):
    """
    True for the ~624-byte '페이지 이동중' wrong-node redirect stub (and any
    suspiciously tiny response) — treated as a fetch failure upstream.
    """
    if text is None:
        return True
    if len(text) < TINY_STUB_BYTES:
        return True
    return False


def parse_dtl(dtl_html):
    """
    Parse a detail page → dict with:
        pdf_url   (absolute /LCMS/DWN.jsp URL scraped from the page, NEVER
                   constructed; the fileName is non-uniform), or None
        title     (full, untruncated title from the view table), or None
    """
    pdf_url = None
    hm = _DWN_HREF_RE.search(dtl_html or "")
    if hm:
        href = _html.unescape(hm.group(1))
        pdf_url = urljoin(BASE, href)

    title = None
    tm = _DTL_TITLE_RE.search(dtl_html or "")
    if tm:
        title = _strip(tm.group(1)) or None

    return {"pdf_url": pdf_url, "title": title}


# ── synopsis (PDF text) ─────────────────────────────────────────────────────


def normalize_case_number(raw):
    """'AAR 2404' / 'AAR-2404' / 'air1906' → 'aar2404' / 'air1906'."""
    if not raw:
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
    m = re.match(r"^(a[ai]r)(\d{3,4})$", compact)
    return f"{m.group(1)}{m.group(2)}" if m else None


def extract_case_number(text):
    """
    Canonical ARAIB case number from the PDF synopsis text → normalised
    ('aar2404' / 'air1906'), or None. The labelled form
    ('Accident Number: AAR2404') is preferred over the header form
    ('ARAIB/AAR2203').
    """
    m = _CASE_LABEL_RE.search(text or "")
    if m:
        out = normalize_case_number(m.group(1))
        if out:
            return out
    m = _CASE_ARAIB_RE.search(text or "")
    if m:
        return normalize_case_number(m.group(1))
    return None


def case_id_from(idx, case_number):
    """
    Canonical case_id = the normalised case number; FALLBACK = 'araib-{idx}'
    when the synopsis carried no extractable case number.
    """
    return case_number or f"araib-{idx}"


def extract_registration(text):
    """Korean HL-registration from text (HL8088 / HL-7525 → 'HL8088')."""
    m = _REG_RE.search(text or "")
    if not m:
        return None
    return m.group(0).upper().replace("-", "")


def extract_event_date(text):
    """
    Occurrence date from the PDF synopsis (NOT the publish date) → ISO
    'YYYY-MM-DD', or None. Tries 'Month D, Year' then 'D Month, Year'.
    """
    if not text:
        return None
    m = _DATE_MDY_RE.search(text)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        day, year = int(m.group(2)), m.group(3)
        if mon and 1 <= day <= 31:
            return f"{year}-{mon:02d}-{day:02d}"
    n = _DATE_DMY_RE.search(text)
    if n:
        day = int(n.group(1))
        mon = _MONTHS.get(n.group(2)[:3].lower())
        year = n.group(3)
        if mon and 1 <= day <= 31:
            return f"{year}-{mon:02d}-{day:02d}"
    return None


def _first_line_value(rx, text):
    m = rx.search(text or "")
    if not m:
        return None
    val = _strip(m.group(1))
    # Trim trailing parenthetical/flight-number noise on a long capture.
    return val or None


def extract_operator(text):
    return _first_line_value(_OPERATOR_RE, text)


def extract_aircraft(text):
    return _first_line_value(_AIRCRAFT_RE, text)


def extract_location(text):
    return _first_line_value(_LOCATION_RE, text)


def report_type_from(title, text):
    """
    'Preliminary' / 'Final' / None from the title or synopsis wording.
    ARAIB publishes only preliminary or final reports, so a non-preliminary
    document that self-identifies as a report ('Aircraft Accident Report',
    'Aircraft Serious Incident Report', 'Final ... Report', 'Interim Report')
    is treated as Final.
    """
    blob = f"{title or ''}\n{(text or '')[:600]}".lower()
    if "preliminary" in blob or "interim" in blob:
        return "Preliminary"
    if "final" in blob:
        return "Final"
    if "report" in blob:
        return "Final"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url, retries=RETRIES):
    """
    GET a board page with cold-TLS-reset retry/backoff. The persistent
    Client's cookie jar transparently replays the WebtoB TMOSH 307 handshake.
    A tiny (<TINY_STUB_BYTES) body is the wrong-node redirect stub → retried,
    then raised as a failure.
    """
    import time
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            if looks_like_stub(resp.text):
                raise RuntimeError(
                    f"tiny stub ({len(resp.text)} B) — wrong-node redirect?"
                )
            return resp.text
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(BACKOFF * (attempt + 1))
    raise last


def download_pdf(client, url, dest_path, retries=RETRIES):
    """Download a PDF with the same cold-TLS-reset retry/backoff."""
    import time
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return dest_path
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(BACKOFF * (attempt + 1))
    raise last
