# bfu_ingest/bfu.py
"""
GSB (Government Site Builder) pagination discovery for BFU Untersuchungsberichte.

The BFU search page is server-rendered HTML with no anti-bot beyond rate-limiting.
PDF links appear directly in each listing row — no detail-page hop required.

Pagination uses ?gtp=<TOKEN>_list%3D<N> query parameters.  The TOKEN and the
highest page number are read from the page-1 pagination hrefs; never hardcoded.

BFU rate-limits aggressively → CAPTCHA after rapid requests.  Always throttle
(DELAY ≥ 3 s between page fetches).
"""
import html as _html
import re
import time

BASE = "https://www.bfu-web.de"
SEARCH = BASE + "/SiteGlobals/Forms/Suche/Untersuchungsberichtesuche_Formular.html"
HEADERS = {"User-Agent": "bfu-ingest/1.0"}
DELAY = 3.0   # politeness — BFU captchas on rapid requests


# ──────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────────────────────────────────────

# Match a teaser Publication row block.  We capture everything inside the outer
# <div> up to the next </div> boundary that closes the row.  Because nested
# divs exist we find each row by its opening tag and then scan for the PDF href.
_ROW_RE = re.compile(
    r'<div[^>]+class="[^"]*teaser type-1 Publication row[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.DOTALL,
)

# Absolute or root-relative PDF href inside a row:
#   /DE/Publikationen/Untersuchungsberichte/<YEAR>/[F]Bericht_<AZ>_<MODEL>_<LOC>.pdf?...
_PDF_HREF_RE = re.compile(
    r'href="(/DE/Publikationen/Untersuchungsberichte/[^"]+\.pdf[^"]*)"'
)

# Link text (first <a>…</a> in the row, for title extraction)
_LINK_TEXT_RE = re.compile(r'<a[^>]+>(.*?)</a>', re.DOTALL)

# Pagination hrefs: ?gtp=<TOKEN>_list%253D<N>  (double-encoded %25 = %, so %253D = %3D = =)
# Also allow single-encoded %3D (some GSB versions emit it unescaped in href attr).
_GTP_RE = re.compile(
    r'[?&]gtp=([0-9]+)_list(?:%253D|%3D|=)(\d+)',
    re.IGNORECASE,
)


# ──────────────────────────────────────────────────────────────────────────────
# Public parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _stem_from_path(path: str) -> str:
    """
    Extract the filename stem (without .pdf) from a URL path.

    e.g. '/DE/.../FBericht_23-0022-1X_Learjet35A_Rendsburg.pdf?...'
         → 'FBericht_23-0022-1X_Learjet35A_Rendsburg'
    """
    # Strip query string
    pure = path.split("?")[0]
    # Last path segment, no extension
    filename = pure.rsplit("/", 1)[-1]
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]
    return filename


def _case_id_from_stem(stem: str) -> str:
    """
    Derive a stable case_id string from the filename stem.

    The stem looks like: Bericht_23-0022-1X_Learjet35A_Rendsburg
                     or: FBericht_24-0173-3X_Learjet35A_Rendsburg
    The aktenzeichen token is the first underscore-delimited segment that
    matches NN-NNNN-NX (two digits, dash, four digits, dash, digit+letter).

    We return "BFU" + that token for consistency with the canonical Aktenzeichen
    printed inside the PDF (e.g. "BFU23-0022-1X").

    Falls back to the full stem if no token is found.
    """
    _AZ_RE = re.compile(r'(\d{2}-\d{4}-\d[A-Z])', re.IGNORECASE)
    m = _AZ_RE.search(stem)
    if m:
        return "BFU" + m.group(1).upper()
    return stem


def _title_from_row(row_html: str) -> str:
    """Extract and clean link text from a row fragment."""
    m = _LINK_TEXT_RE.search(row_html)
    if not m:
        return ""
    raw = m.group(1)
    # Strip inner tags
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = _html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def parse_pdf_links(html: str) -> list[dict]:
    """
    Return a list of dicts for every BFU Untersuchungsbericht found on the page.

    Each dict has:
        pdf_url   – absolute URL to the PDF
        filename  – filename stem (stable, unique key for the staging table)
        case_id   – normalized Aktenzeichen string, e.g. "BFU23-0022-1X"
        title     – text content of the row's link, HTML-unescaped
    """
    results = []
    for m in _ROW_RE.finditer(html):
        row_html = m.group(0)
        href_m = _PDF_HREF_RE.search(row_html)
        if not href_m:
            continue
        href = _html.unescape(href_m.group(1))
        pdf_url = href if href.startswith("http") else BASE + href
        stem = _stem_from_path(href)
        results.append({
            "pdf_url": pdf_url,
            "filename": stem,
            "case_id": _case_id_from_stem(stem),
            "title": _title_from_row(row_html),
        })
    return results


def gtp_token(html: str) -> str | None:
    """
    Extract the GSB list-widget token from pagination hrefs on page 1.

    Returns the token string (e.g. "675276"), or None if no pagination found.
    The token is the numeric part before the underscore in ?gtp=<TOKEN>_list….
    """
    m = _GTP_RE.search(html)
    return m.group(1) if m else None


def last_page(html: str) -> int:
    """
    Return the highest page number found in pagination hrefs.

    Returns 1 if no pagination links are present (single-page result set).
    """
    pages = [int(m.group(2)) for m in _GTP_RE.finditer(html)]
    return max(pages) if pages else 1


def page_url(token: str, n: int) -> str:
    """
    Build the URL for page N of the BFU search results (N ≥ 2; page 1 = bare SEARCH).

    GSB encodes the list param as: ?gtp=<TOKEN>_list%3D<N>
    (The %3D is the URL-encoded '=' character.)
    """
    return f"{SEARCH}?gtp={token}_list%3D{n}"


def iter_reports(client, max_pages: int | None = None):
    """
    Enumerate BFU Untersuchungsberichte by walking the paginated search results.

    Strategy:
    - GET page 1 (bare SEARCH URL); parse PDF rows; read token + last_page.
    - For pages 2..last_page: sleep(DELAY); GET page_url(token, n); parse rows.
    - De-duplicate by pdf_url throughout.
    - Yields dicts: {pdf_url, filename, case_id, title}

    client: httpx.Client (or compatible) — should carry HEADERS.
    max_pages: cap total pages fetched (for smoke testing); None = fetch all.
    """
    seen: set[str] = set()
    pages_fetched = 0

    # ── Page 1 ────────────────────────────────────────────────────────────────
    resp = client.get(SEARCH)
    resp.raise_for_status()
    html = resp.text
    pages_fetched += 1

    for row in parse_pdf_links(html):
        key = row["pdf_url"]
        if key not in seen:
            seen.add(key)
            yield row

    if max_pages is not None and pages_fetched >= max_pages:
        return

    token = gtp_token(html)
    if token is None:
        return  # single page — no pagination

    total_pages = last_page(html)

    # ── Pages 2..total_pages ──────────────────────────────────────────────────
    for n in range(2, total_pages + 1):
        time.sleep(DELAY)
        resp = client.get(page_url(token, n))
        resp.raise_for_status()
        html = resp.text
        pages_fetched += 1

        for row in parse_pdf_links(html):
            key = row["pdf_url"]
            if key not in seen:
                seen.add(key)
                yield row

        if max_pages is not None and pages_fetched >= max_pages:
            return


def download(client, url: str, dest: str):
    """GET url and write bytes to dest path.  Caller is responsible for throttling."""
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
