# bfu_ingest/bea.py
"""TYPO3 HTML scraper for bea.aero notified-events listing."""
import html as _html
import re

BASE = "https://bea.aero"
LANDING = BASE + "/en/investigation-reports/notified-events/"
HEADERS = {"User-Agent": "bfu-ingest/1.0"}

# ──────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────

_DETAIL_RE = re.compile(r"/detail/([^/?#]+)/?")

# Matches every <h1 class="search-entry__title"> … </h1> block
_H1_RE = re.compile(
    r'<h1[^>]+class="search-entry__title"[^>]*>(.*?)</h1>',
    re.DOTALL,
)
# Pulls the href and title attributes out of an <a …> tag
_HREF_RE = re.compile(r'href="([^"]*)"')
_TITLE_ATTR_RE = re.compile(r'title="([^"]*)"')

# Year-facet <a> links
_YEAR_FACET_RE = re.compile(
    r'href="([^"]*facetTitle%5D=year_intS[^"]*)"',
)

# Paginator links – page-N links only (current=page1 has no page param)
_PAGE_LINK_RE = re.compile(
    r'href="([^"]*tx_news_pi1%5Bpage%5D=(\d+)[^"]*)"',
)

# PDF inside /fileadmin/
_PDF_RE = re.compile(r'href="(/fileadmin/[^"]+\.pdf)"')


def slug_from_detail_url(url: str) -> str:
    """Return the last non-empty path segment of a /detail/<slug>/ URL."""
    m = _DETAIL_RE.search(url or "")
    return m.group(1) if m else ""


def _abs(href: str) -> str:
    """Make an href absolute and html-unescape it."""
    href = _html.unescape(href or "")
    if href.startswith("/"):
        return BASE + href
    return href


def parse_rows(html_text: str) -> list:
    """
    Return [{detail_url, title}, …] for every search-entry__title h1 on the page.
    Only rows whose href contains /detail/ are included.
    """
    rows = []
    for m in _H1_RE.finditer(html_text):
        inner = m.group(1)
        href_m = _HREF_RE.search(inner)
        if not href_m:
            continue
        href = _html.unescape(href_m.group(1))
        if "/detail/" not in href:
            continue
        # Prefer the title attribute; fall back to link text
        title_m = _TITLE_ATTR_RE.search(inner)
        if title_m:
            title = _html.unescape(title_m.group(1)).strip()
        else:
            title = re.sub(r"<[^>]+>", " ", inner)
            title = re.sub(r"\s+", " ", title).strip()
        rows.append({"detail_url": href, "title": title})
    return rows


def year_facet_links(html_text: str) -> list:
    """
    Return list of absolute URLs for all year_intS facet links on the page.
    """
    return [_abs(m.group(1)) for m in _YEAR_FACET_RE.finditer(html_text)]


def next_page_link(html_text: str, current_page: int) -> str | None:
    """
    From the f3-widget-paginator, return the absolute URL for page
    (current_page + 1), or None if that link is absent (last page).

    The paginator is a SLIDING WINDOW: from page N it shows links for
    approximately N, N+1, N+2, … so the next link is always present
    until the final page.
    """
    target = current_page + 1
    for m in _PAGE_LINK_RE.finditer(html_text):
        if int(m.group(2)) == target:
            return _abs(m.group(1))
    return None


def iter_events(client, _max: int = None, _max_pages: int = None):
    """
    Enumerate ALL events by walking the GLOBAL newest-first paginated list.

    Strategy (proven live):
    - GET the landing page → parse_rows → global page 1 (10 newest events).
      The landing page also carries a global paginator with the cHash needed
      to continue — no year-facet redirection required.
    - Walk pages 2, 3, 4, … using next_page_link(html, current_page) until
      no next link exists.
    - De-duplicate slugs throughout.

    NOTE: Year-facet links must NOT be followed for pagination — a year-facet
    page's paginator returns only that year's events, not the global list.
    The landing page's paginator uses bare ?tx_news_pi1[page]=N links that
    advance the GLOBAL newest-first sequence.

    client: an httpx.Client (or compatible) with HEADERS already set.
    _max: cap on total events yielded (for smoke-testing).
    _max_pages: cap on total pages fetched (for smoke-testing / live validation).
    """
    seen_slugs: set = set()
    total = 0
    pages_fetched = 0

    def _yield_rows(page_html):
        nonlocal total
        for row in parse_rows(page_html):
            slug = slug_from_detail_url(row["detail_url"])
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            yield {
                "slug": slug,
                "detail_url": row["detail_url"],
                "title": row["title"],
            }
            total += 1
            if _max is not None and total >= _max:
                return

    # ── Step 1: Fetch landing page → global page 1 ──
    resp = client.get(LANDING)
    resp.raise_for_status()
    current_html = resp.text
    current_page = 1
    pages_fetched += 1

    yield from _yield_rows(current_html)
    if _max is not None and total >= _max:
        return
    if _max_pages is not None and pages_fetched >= _max_pages:
        return

    # ── Step 2: Walk global pages 2, 3, 4, … using landing's paginator ──
    while True:
        next_url = next_page_link(current_html, current_page)
        if next_url is None:
            break  # reached the last page

        resp = client.get(next_url)
        resp.raise_for_status()
        current_html = resp.text
        current_page += 1
        pages_fetched += 1

        rows_before = total
        yield from _yield_rows(current_html)
        if _max is not None and total >= _max:
            return

        # Safety: if both 0 rows AND no next link → stop
        if total == rows_before and next_page_link(current_html, current_page) is None:
            break

        if _max_pages is not None and pages_fetched >= _max_pages:
            return


def get_detail_pdf_url(client, detail_url: str) -> str | None:
    """
    GET BASE+detail_url and return the first /fileadmin/…pdf href as an
    absolute URL, or None if no PDF link is found.
    """
    url = detail_url if detail_url.startswith("http") else BASE + detail_url
    resp = client.get(url)
    resp.raise_for_status()
    m = _PDF_RE.search(resp.text)
    return (BASE + m.group(1)) if m else None


def download(client, url: str, dest: str):
    """Download url to dest (same signature as govuk.download)."""
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
