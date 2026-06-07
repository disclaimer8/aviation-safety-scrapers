# sub_ingest/sub.py
"""
SUB Austria (Sicherheitsuntersuchungsstelle des Bundes, bmimi.gv.at) aviation
accident-report crawler/parser.

Source shape (verified live 2026-06-05):
    hub  /sub/berichte/luftfahrt.html  → 8 CATEGORY pages
        /sub/berichte/luftfahrt/{cat}.html  for cat in:
            motorflugzeuge, motorsegler, segelflugzeuge, hubschrauber,
            ultraleichtflugzeuge, heissluftballons,
            fallschirme-haenge-paragleiter, international

TWO category layouts (MUST branch):
  • YEAR-BASED (motorflugzeuge, motorsegler, segelflugzeuge, hubschrauber):
      category page → YEAR links /{cat}/{YYYY}.html  (read the actual list;
      gaps exist — never assume a range). Year page → REPORT links
      /{cat}/{YYYY}/{MMDD}_{aircraft}_{caseid}.html
  • FLAT (ultraleichtflugzeuge, heissluftballons,
      fallschirme-haenge-paragleiter, international):
      category page lists REPORT links directly; slug starts with full
      YYYYMMDD.

⚠️ GET only — NEVER HEAD (HEAD → 302 → 403). Gate on HTTP status (the 404 page
is ~290 KB but returns 404). Browser UA. No anti-bot; throttle is courtesy.

Report page (main#content):
    time[datetime="YYYY-MM-DD"] = occurrence date (gold standard)
    span.title                  = aircraft type  (strip &#xa0; / nested lang spans)
    span.subtitle > abbr[title="Geschäftszahl"] "GZ …" = GZ file number
                                  (e.g. 2025-0.211.836; sometimes ABSENT)
    p.abstract                  = location line
    <p> siblings BETWEEN p.abstract and div.infobox = German SUMMARY (~1-1.4 K
                                  chars — a SUMMARY, not the full narrative)
    div.infobox a.file[href]    = report-type label (Abschlussbericht /
                                  Vereinfachter Untersuchungsbericht /
                                  Untersuchungsbericht — drop Zwischenbericht)
                                  + PDF link /dam/jcr:{UUID}/{file}.pdf (≤18 MB)

FULL narrative + OE- registration are PDF-ONLY (extracted at fetch).

⚠️ case_id: the slug's trailing numeric is NON-UNIQUE and many slugs lack a
clean numeric; the _en/_de suffix is part of the slug (content is ALWAYS
German). PRIMARY KEY = derive from the full relative path: strip the
'/sub/berichte/luftfahrt/' prefix and the '.html' suffix, replace '/' with
'--', lowercase. Verified unique 231/231. Also store the raw URL.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://www.bmimi.gv.at"
HUB = "/sub/berichte/luftfahrt.html"
DELAY = 1.0

# The 8 categories (order = hub order). Year-based vs flat is detected at parse
# time (presence of /{cat}/{YYYY}.html year links), so this set is informational.
CATEGORIES = [
    "motorflugzeuge",
    "motorsegler",
    "segelflugzeuge",
    "hubschrauber",
    "ultraleichtflugzeuge",
    "heissluftballons",
    "fallschirme-haenge-paragleiter",
    "international",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_LF_PREFIX = "/sub/berichte/luftfahrt/"

# Report-type labels we keep (Zwischenbericht = interim, dropped).
_REPORT_KINDS = (
    "Abschlussbericht",
    "Vereinfachter Untersuchungsbericht",
    "Untersuchungsbericht",
)
_DROP_KINDS = ("Zwischenbericht",)

# Austrian civil registration inside the PDF text layer (OE-XXX). Foreign-
# registered aircraft legitimately yield None.
_REG_RE = re.compile(r"\bOE-[A-Z0-9]{3}\b")


def _strip(fragment):
    """Tags out, entities unescaped (incl. &#xa0;), whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _hub_url():
    return urljoin(BASE, HUB)


def category_url(cat):
    return urljoin(BASE, f"{_LF_PREFIX}{cat}.html")


# ──────────────────────────────────────────────────────────────────────────────
# Hub / category / year link harvesting
# ──────────────────────────────────────────────────────────────────────────────


def parse_hub(hub_html):
    """Hub HTML → ordered, de-duped list of category slugs found as
    /sub/berichte/luftfahrt/{cat}.html links."""
    out = []
    seen = set()
    for m in re.finditer(
        r'href="' + re.escape(_LF_PREFIX) + r'([a-z-]+)\.html"',
        hub_html or "",
    ):
        cat = m.group(1)
        if cat not in seen:
            seen.add(cat)
            out.append(cat)
    return out


def parse_year_links(cat, cat_html):
    """Category HTML → sorted list of (year, year_url) for /{cat}/{YYYY}.html.
    Empty list ⇒ this is a FLAT category (no year layer). Reads the actual
    year list (gaps exist); never assumes a range."""
    pat = re.compile(
        r'href="(' + re.escape(_LF_PREFIX + cat) + r'/(\d{4})\.html)"'
    )
    years = {}
    for m in pat.finditer(cat_html or ""):
        href, year = m.group(1), m.group(2)
        years[year] = urljoin(BASE, href)
    return sorted(years.items())


def parse_report_links(cat, page_html):
    """Harvest report-detail links for a category/year page.

    YEAR-based report path: /{cat}/{YYYY}/{slug}.html
    FLAT report path:       /{cat}/{slug}.html      (slug starts YYYYMMDD)

    Returns ordered, de-duped list of absolute report URLs. Year-index links
    (/{cat}/{YYYY}.html) are excluded.
    """
    out = []
    seen = set()
    base = re.escape(_LF_PREFIX + cat)
    # Year-based: {cat}/{YYYY}/{slug}.html  (slug must not be empty)
    year_pat = re.compile(r'href="(' + base + r'/\d{4}/[^"/]+\.html)"')
    # Flat: {cat}/{slug}.html where slug is NOT a bare 4-digit year.
    flat_pat = re.compile(r'href="(' + base + r'/([^"/]+)\.html)"')

    for m in year_pat.finditer(page_html or ""):
        href = urljoin(BASE, m.group(1))
        if href not in seen:
            seen.add(href)
            out.append(href)
    for m in flat_pat.finditer(page_html or ""):
        slug = m.group(2)
        if re.fullmatch(r"\d{4}", slug):
            continue  # year-index link, not a report
        href = urljoin(BASE, m.group(1))
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# case_id derivation (PRIMARY KEY)
# ──────────────────────────────────────────────────────────────────────────────


def case_id_from_url(url):
    """Derive the stable case_id from a report URL.

    Strip the '/sub/berichte/luftfahrt/' prefix and the '.html' suffix, replace
    '/' with '--', lowercase. Verified unique 231/231 (the slug's trailing
    numeric is NOT unique; '_en'/'_de' are part of the slug, not a language
    toggle). Accepts absolute or relative URLs.
        '/sub/berichte/luftfahrt/motorflugzeuge/2024/0330_cirrus-sr20_85305.html'
            → 'motorflugzeuge--2024--0330_cirrus-sr20_85305'
    """
    path = url or ""
    path = re.sub(r"^https?://[^/]+", "", path)
    i = path.find(_LF_PREFIX)
    if i != -1:
        path = path[i + len(_LF_PREFIX):]
    if path.endswith(".html"):
        path = path[:-5]
    path = path.strip("/")
    return path.replace("/", "--").lower()


def category_of(case_id):
    """The category slug (first '--' segment) of a derived case_id."""
    return (case_id or "").split("--", 1)[0]


def year_of(case_id):
    """The 4-digit year segment of a year-based case_id, else None."""
    parts = (case_id or "").split("--")
    if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[1]):
        return parts[1]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Report detail page parse
# ──────────────────────────────────────────────────────────────────────────────

_MAIN_RE = re.compile(r'<main[^>]*id="content".*?</main>', re.S | re.I)
_TIME_RE = re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"', re.S | re.I)
_TITLE_RE = re.compile(r'<span class="title">(.*?)</span>\s*</', re.S | re.I)
_SUBTITLE_RE = re.compile(r'<span class="subtitle">(.*?)</span>', re.S | re.I)
_ABSTRACT_RE = re.compile(r'<p class="abstract">(.*?)</p>', re.S | re.I)
_INFOBOX_RE = re.compile(r'<div class="infobox".*?</div>', re.S | re.I)
_FILE_A_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]+\.pdf)"[^>]*\bclass="file"[^>]*>(.*?)</a>'
    r'|<a\b[^>]*\bclass="file"[^>]*\bhref="([^"]+\.pdf)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)


def _main_block(page_html):
    m = _MAIN_RE.search(page_html or "")
    return m.group(0) if m else (page_html or "")


def _title_span(content):
    # span.title may wrap nested <span lang=...> chunks; the regex above stops
    # at the FIRST </span>, which can be a nested one. Match the OUTER span by
    # taking everything up to the closing </span> that precedes the closing
    # </div> of the title block instead.
    m = re.search(r'<span class="title">(.*?)</span>\s*</div>', content, re.S | re.I)
    if not m:
        m = re.search(r'<span class="title">(.*?)</span>', content, re.S | re.I)
    return _strip(m.group(1)) if m else None


def parse_report(page_html):
    """Parse a report-detail page → dict of metadata + HTML summary.

    Keys: event_date, aircraft, gz, location, summary_text, report_kind,
    pdf_url. Missing fields are None (GZ, PDF, report_kind can all be absent).
    summary_text = the <p> paragraphs BETWEEN p.abstract and div.infobox.
    """
    content = _main_block(page_html)

    md = _TIME_RE.search(content)
    event_date = md.group(1) if md else None

    aircraft = _title_span(content)

    gz = None
    ms = _SUBTITLE_RE.search(content)
    if ms:
        sub = _strip(ms.group(1))
        # 'GZ 2025-0.211.836' → drop the 'GZ' token.
        m = re.search(r"GZ\s*([0-9][0-9.\-]*)", sub)
        if m:
            gz = m.group(1)
        elif sub:
            gz = sub or None

    location = None
    ma = _ABSTRACT_RE.search(content)
    abstract_end = None
    if ma:
        location = _strip(ma.group(1)) or None
        abstract_end = ma.end()

    info = _INFOBOX_RE.search(content)
    infobox_start = info.start() if info else None

    # Summary = <p> paragraphs strictly between abstract and infobox.
    summary_text = None
    if abstract_end is not None:
        region_end = infobox_start if infobox_start is not None else len(content)
        region = content[abstract_end:region_end]
        paras = [_strip(p) for p in _P_RE.findall(region)]
        paras = [p for p in paras if p]
        if paras:
            summary_text = "\n\n".join(paras)

    report_kind = None
    pdf_url = None
    if info:
        block = info.group(0)
        fm = _FILE_A_RE.search(block)
        if fm:
            href = fm.group(1) or fm.group(3)
            label_html = fm.group(2) or fm.group(4) or ""
            pdf_url = urljoin(BASE, href) if href else None
            report_kind = _report_kind_from_label(label_html)

    return {
        "event_date": event_date,
        "aircraft": aircraft,
        "gz": gz,
        "location": location,
        "summary_text": summary_text,
        "report_kind": report_kind,
        "pdf_url": pdf_url,
    }


def _report_kind_from_label(label_html):
    """Extract the report-type label from the file-link inner HTML. The PDF
    'fileinfo' span and the quoted aircraft name are stripped; only the leading
    report-type phrase is returned. Zwischenbericht (interim) → None."""
    # Drop the trailing fileinfo span and everything after it.
    txt = re.sub(r'<span class="fileinfo".*$', "", label_html or "", flags=re.S | re.I)
    txt = _strip(txt)
    if not txt:
        return None
    for drop in _DROP_KINDS:
        if txt.startswith(drop):
            return None
    for kind in _REPORT_KINDS:
        if txt.startswith(kind):
            return kind
    # Fallback: the phrase before the first „ (aircraft quote) or end.
    head = re.split(r"[„„]", txt, 1)[0].strip()
    return head or None


def extract_registration(text):
    """Best-effort OE- registration from PDF text; None when absent (foreign)."""
    m = _REG_RE.search(text or "")
    return m.group(0).upper() if m else None


# ──────────────────────────────────────────────────────────────────────────────
# Card metadata (year/flat listing card) — optional enrichment at discover
# ──────────────────────────────────────────────────────────────────────────────

_CARD_RE = re.compile(
    r'<li class="col-12 overview-item.*?</li>', re.S | re.I
)
_CARD_LINK_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*class="card-link"', re.S | re.I)
_CARD_DATE_RE = re.compile(r'<small class="card-date">(.*?)</small>', re.S | re.I)
_CARD_TITLE_RE = re.compile(r'<h2 class="card-title-heading">(.*?)</h2>', re.S | re.I)
_CARD_TEXT_RE = re.compile(r'<p class="card-text">(.*?)</p>', re.S | re.I)


def parse_cards(page_html):
    """Parse listing cards → {report_url: {aircraft, location, card_date}}.

    Used only as best-effort enrichment; report-detail parse is authoritative.
    """
    out = {}
    for cm in _CARD_RE.finditer(page_html or ""):
        card = cm.group(0)
        lm = _CARD_LINK_RE.search(card)
        if not lm:
            continue
        url = urljoin(BASE, _html.unescape(lm.group(1)))
        tm = _CARD_TITLE_RE.search(card)
        xm = _CARD_TEXT_RE.search(card)
        dm = _CARD_DATE_RE.search(card)
        out[url] = {
            "aircraft": _strip(tm.group(1)) if tm else None,
            "location": _strip(xm.group(1)) if xm else None,
            "card_date": _strip(dm.group(1)) if dm else None,
        }
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url):
    """GET only (NEVER HEAD). Raises for non-2xx; gates on status code (the
    404 page is large but correctly returns HTTP 404)."""
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
