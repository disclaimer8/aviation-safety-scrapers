# ansv_ingest/ansv.py
"""ANSV (Italy) — WordPress category archive scraper.

Enumeration: paginated category archive at BASE/category/relazioni-dinchiesta/
Each page lists ~10 report cards; each card links to a single-report page
that contains a PDF link (Relazione*.pdf) with the full investigation report.

HTML structure (verified against live fixtures 2026-06-03):
  Listing: <article class="card …"> contains
    - <span class="data">DD Mon YYYY</span>   ← publication date
    - <h5 class="card-title …">title</h5>     ← title (location, aircraft, reg)
    - <div class="card-text"><p>…</p>          ← body with aircraft/reg/date
    - <a href="https://ansv.it/<slug>/" class="read-more">  ← report page URL
  Pagination: <ul class='page-numbers'> with <a class="page-numbers" href=".../page/N/">

  Report page: single <article class="card …"> containing
    - <h1 class="entry-title">…title…</h1>
    - <div class="entry-content">  with first few <p> paragraphs:
        para 0: "RELAZIONE DI INCHIESTA"
        para 1: "Incidente occorso … marche … in data DD/MM/YYYY"
    - <a href="…Relazione….pdf">  ← PDF link somewhere in entry-content

Italian body-text patterns used for extraction (all defensive / None on miss):
  registration:  marche [di registrazione|di identificazione]? <REG>
  incident date: (in data|il) DD/MM/YYYY
  aircraft type: occorso <articles+nouns>? <TYPE> marche  (stripped of Italian
                 articles + generic nouns: all'elicottero / aeromobile / etc.)
  location:      from report page title (comma-separated prefix)
"""

import re
from urllib.parse import urljoin, urlparse

BASE = "https://ansv.it"
LISTING_URL = "https://ansv.it/category/relazioni-dinchiesta/"
DELAY = 2.0
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

# ── Italian article/noun prefixes to strip from extracted aircraft type ───────
_AC_STRIP = re.compile(
    r"^(?:all[a-z’`´'']* |al |alla |agli aeromobili |agli |"
    r"l[a-z’`´'']* |il |la |lo |i )?"
    r"(?:elicottero |aeromobile |aliante |motoaliante |velivolo |aeronave |mongolfiera )?",
    re.IGNORECASE,
)

# incident date patterns: "in data DD/MM/YYYY" or "il DD/MM/YYYY"
_DATE_RE = re.compile(r"(?:in data|il)\s+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)

# registration mark after "marche [di registrazione|di identificazione]?"
_REG_RE = re.compile(
    r"marche(?:\s+di\s+(?:registrazione|identificazione))?\s+([A-Z0-9][A-Z0-9-]+)",
    re.IGNORECASE,
)

# aircraft type: everything between "occorso" and first "marche"
_AC_TYPE_RE = re.compile(r"occorso\s+(.+?)\s+marche", re.IGNORECASE)

# pagination: highest page number in category archive
_PAGE_NUM_RE = re.compile(
    r"/category/relazioni-dinchiesta/page/(\d+)/", re.IGNORECASE
)

# PDF link: prefer files with "relazione" in the name
_PDF_RE = re.compile(r'href="([^"]*\.pdf)"', re.IGNORECASE)
_RELAZIONE_RE = re.compile(r"relazione", re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_tags(s: str) -> str:
    """Remove HTML tags; decode common entities inline."""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&lt;", "<", s)
    s = re.sub(r"&gt;", ">", s)
    s = re.sub(r"&#8217;|&#x2019;|’", "'", s)
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"&#\d+;", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _date_iso(raw: str) -> str | None:
    """Convert DD/MM/YYYY → YYYY-MM-DD; return None on failure."""
    raw = (raw or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", raw)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _abs_url(url: str) -> str:
    """Make URL absolute against BASE."""
    if url.startswith("http"):
        return url
    return urljoin(BASE, url)


# ── Public API ────────────────────────────────────────────────────────────────

def page_url(n: int) -> str:
    """Return the listing URL for page n (page 1 == LISTING_URL)."""
    if n <= 1:
        return LISTING_URL
    return f"{BASE}/category/relazioni-dinchiesta/page/{n}/"


def last_page(listing_html: str) -> int:
    """
    Parse the pagination block and return the highest page number found.
    Falls back to 1 if no page links are found.
    """
    nums = [int(m) for m in _PAGE_NUM_RE.findall(listing_html)]
    return max(nums) if nums else 1


def parse_listing(html: str) -> list[dict]:
    """
    Parse one listing page.  Returns a list of dicts (one per article):

        report_url          str  – absolute URL to the report page
        title               str | None
        aircraft            str | None
        registration        str | None
        date_of_occurrence  str | None – ISO YYYY-MM-DD
        location            str | None
    """
    results = []
    articles = re.findall(r"<article[^>]*>.*?</article>", html, re.DOTALL)

    for article in articles:
        # --- report_url: the "Leggi di più" / "read-more" anchor ---
        link_m = re.search(
            r'<a\s[^>]*href="(https://ansv\.it/[^"]+/)"[^>]*class="read-more"',
            article,
        )
        if not link_m:
            # fallback: any ansv.it/<slug>/ link that is NOT a category
            link_m = re.search(
                r'href="(https://ansv\.it/(?!category/|wp-content/)[^"]+/)"[^>]*title=',
                article,
            )
        if not link_m:
            continue
        report_url = link_m.group(1)

        # --- title ---
        title_m = re.search(r"<h5[^>]*>(.*?)</h5>", article, re.DOTALL)
        title = _strip_tags(title_m.group(1)) if title_m else None

        # --- body text: first <p> inside card-text ---
        body_m = re.search(
            r'<div[^>]*class="card-text"[^>]*>\s*<p>(.*?)</p>', article, re.DOTALL
        )
        body = _strip_tags(body_m.group(1)) if body_m else ""

        # --- incident date from body ---
        date_raw_m = _DATE_RE.search(body)
        date_of_occurrence = _date_iso(date_raw_m.group(1)) if date_raw_m else None

        # --- registration mark from body (first match) ---
        reg_m = _REG_RE.search(body)
        registration = reg_m.group(1).upper() if reg_m else None

        # --- aircraft type from body ---
        ac_m = _AC_TYPE_RE.search(body)
        aircraft = None
        if ac_m:
            raw_ac = ac_m.group(1).strip()
            aircraft = _AC_STRIP.sub("", raw_ac).strip() or None

        # --- location: leading part of the title before the first comma ---
        location = None
        if title:
            comma_idx = title.find(",")
            if comma_idx > 0:
                location = title[:comma_idx].strip()

        results.append(
            {
                "report_url": report_url,
                "title": title,
                "aircraft": aircraft,
                "registration": registration,
                "date_of_occurrence": date_of_occurrence,
                "location": location,
            }
        )

    return results


def parse_report(html: str) -> dict:
    """
    Parse a single report page.  Returns:

        pdf_url    str | None  – absolute URL to the investigation PDF
        title      str | None  – page <h1> text
    """
    # --- title from h1.entry-title ---
    title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    title = None
    if title_m:
        title = _strip_tags(title_m.group(1))

    # --- PDF link: prefer one containing "relazione" in the path ---
    all_pdfs = _PDF_RE.findall(html)
    pdf_url = None
    for href in all_pdfs:
        if _RELAZIONE_RE.search(href):
            pdf_url = _abs_url(href)
            break
    # fallback: first .pdf found
    if pdf_url is None and all_pdfs:
        pdf_url = _abs_url(all_pdfs[0])

    return {"pdf_url": pdf_url, "title": title}


def make_case_id(registration: str | None, date_iso: str | None, report_url: str) -> str:
    """
    Build a deterministic case_id.

    Primary:  "<REGISTRATION>_<YYYY-MM-DD>"  when both are present.
    Fallback: slug derived from report_url path  (e.g. "lago-di-varese-...").
    """
    if registration and date_iso:
        return f"{registration}_{date_iso}"
    # derive from URL slug: last non-empty path segment
    path = urlparse(report_url).path.strip("/")
    slug = path.split("/")[-1] if path else "ansv-unknown"
    return slug


def download(client, pdf_url: str, dest: str) -> None:
    """
    Download pdf_url → dest using the provided sync httpx.Client.
    Raises httpx.HTTPStatusError on non-2xx.
    """
    headers = {"User-Agent": UA}
    resp = client.get(pdf_url, headers=headers)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
