# ciaado_ingest/ciaado.py
"""CIAA (Dominican Republic) Phoca Download scraper.

Source: https://ciaa.gob.do  (Joomla + Phoca Download component)

Structure:
  /index.php/informesf lists three top-level Phoca categories:
    - 19-informes                  (final reports, per-year subcategories)
    - 29-informes-preliminares     (preliminary reports, per-year subcats)
    - 40-declaraciones-provisionales (provisional declarations, per-year subcats)
  Each top category page links per-year subcategory pages
    (/index.php/informesf/category/<id>-<year>).
  Each year subcategory page renders one
    <div class="attachment__container_item"> block per report, containing:
      - <div class="icon ... alt=<filename>.pdf>          (file name)
      - <div class="title">Informe Final caso 101-2019 - HI-878</div>
      - an overlib(...) tooltip with 'Tamaño:' and 'Fecha:' (size + upload date)
      - <a class="btn-descargar" href="...?download=<id>:<slug>">Descargar</a>

Download links are gated Phoca '?download=<id>:<slug>' URLs that 302/stream the
actual PDF; we follow the real href verbatim (do NOT construct) and send a
Referer header (the year-category page).  PDFs are Spanish, text-layer expected.

case_id: title carries 'caso [CIAA ]NNN[-YY[YY]]'.  make_case_id normalises to
'CIAA-NNN-YYYY' (whitespace collapsed around '-' / '/'), 2-digit years expanded.
"""
import html as _html
import re

BASE = "https://ciaa.gob.do"
INDEX_URL = BASE + "/index.php/informesf"
REFERER = INDEX_URL
DELAY = 1.8

# Top-level Phoca categories, in dedup priority order:
# Final reports win over Preliminary, which win over Provisional declarations
# for a colliding case number (first walked is kept).
TOP_CATEGORIES = [
    "19-informes",
    "29-informes-preliminares",
    "40-declaraciones-provisionales",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
    "Accept-Language": "es-DO,es;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Subcategory links: /index.php/informesf/category/<id>-<slug>
_SUBCAT_RE = re.compile(
    r'href="((?:https?:)?(?://ciaa\.gob\.do)?/index\.php/informesf/category/'
    r'(\d+)-([^"?]+))"',
    re.IGNORECASE,
)

# A single report row block.
_ROW_BLOCK_RE = re.compile(
    r'<div class="attachment__container_item">(.*?)</div>\s*</div>',
    re.DOTALL,
)
# More robust: split on the marker and process each chunk independently.
_ROW_SPLIT_RE = re.compile(r'<div class="attachment__container_item">')

_TITLE_RE = re.compile(r'<div class="title">(.*?)</div>', re.DOTALL)
_ALT_RE = re.compile(r'alt=([^\s>]+\.pdf)', re.IGNORECASE)
_DOWNLOAD_RE = re.compile(
    r'href="([^"]*\?download=[^"]+)"', re.IGNORECASE
)
# Tooltip 'Fecha:' value, e.g. 'Fecha:&lt;/div&gt;&lt;div class=\'pd-fl-m\'&gt;14 Mayo 2025'
_FECHA_RE = re.compile(
    r"Fecha:.*?pd-fl-m['\"]?&gt;\s*(\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# case_id from a title: 'caso [CIAA ]NNN[-YY[YY]]'
_CASE_RE = re.compile(
    r"caso\b[\s:-]*"          # 'caso' then separators (space / colon / dash)
    r"(?:CIAA[\s-]*)?"        # optional 'CIAA' token
    r"(\d{2,3})"             # case number NNN
    r"(?:\s*[-/]\s*(\d{2,4}))?",  # optional year -YY or -YYYY
    re.IGNORECASE,
)

# Registration: HI-878, HI721, N-9956K, N206BH, C-GOWG, VP-BHB, EC-YJD, LV-CSX,
# HH-CRB, YV 120T … Grab the trailing token after the case number.
_REG_RE = re.compile(
    r"\b((?:[A-Z]{1,2})[\s-]?[A-Z0-9][A-Z0-9-]{1,7})\b"
)

# Spanish months for the upload-date tooltip (NOT the occurrence date).
_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

_YY_CEIL = 27  # 2-digit YY → 2000+YY when <= ceil, else 1900+YY


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# case_id normalisation
# ──────────────────────────────────────────────

def _normalize_case_id(raw):
    """Collapse whitespace around '-' and '/' separators and uppercase.

    'CIAA 101 - 2019' → 'CIAA-101-2019';  'caso 116 / 24' → 'CIAA-116/24'.
    Remaining internal whitespace runs are converted to '-' separators.
    """
    if raw is None:
        return None
    s = _html.unescape(str(raw)).strip()
    if not s:
        return s
    # collapse spaces that sit immediately around a '-' or '/'
    s = re.sub(r"\s*([-/])\s*", r"\1", s)
    # any remaining internal whitespace runs become a single '-' separator
    # (so the space-form 'CIAA NNN YYYY' normalises to 'CIAA-NNN-YYYY')
    s = re.sub(r"\s+", "-", s).strip("-")
    return s.upper()


def make_case_id(title):
    """Build a normalised case_id 'CIAA-NNN-YYYY' from a report title.

    Returns None when the title carries no parseable 'caso NNN' token.
    2-digit years are expanded (<= 27 → 20xx, else 19xx).  When the title has
    no year, the case_id is 'CIAA-NNN' (no year suffix).
    """
    if not title:
        return None
    m = _CASE_RE.search(title)
    if not m:
        return None
    num = m.group(1).zfill(3)
    year = m.group(2)
    if year is not None:
        y = int(year)
        if y < 100:
            y = 2000 + y if y <= _YY_CEIL else 1900 + y
        return _normalize_case_id(f"CIAA {num} {y}")
    return _normalize_case_id(f"CIAA {num}")


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_subcategory_urls(category_html):
    """Parse a top-category page → list of absolute per-year subcategory URLs.

    Excludes self-links back to the three top categories.  Preserves order,
    de-duplicates.
    """
    seen = set()
    urls = []
    top_slugs = {c.split("-", 1)[1] if "-" in c else c for c in TOP_CATEGORIES}
    top_ids = {c.split("-", 1)[0] for c in TOP_CATEGORIES}
    for m in _SUBCAT_RE.finditer(category_html):
        cat_id = m.group(2)
        slug = m.group(3)
        # skip the three top-level categories themselves
        if cat_id in top_ids:
            continue
        if slug in top_slugs:
            continue
        path = "/index.php/informesf/category/%s-%s" % (cat_id, slug)
        url = BASE + path
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ──────────────────────────────────────────────
# Row parsing
# ──────────────────────────────────────────────

def _event_class(title):
    t = (title or "").lower()
    if "preliminar" in t:
        return "Preliminary report"
    if "provisional" in t or "provicional" in t:
        return "Provisional declaration"
    return "Final report"


def _parse_registration(title, case_num):
    """Extract aircraft registration from the title.

    Registration is the trailing token after the case-number block.  We strip
    the leading 'Informe/Declaracion ... caso NNN[-YYYY]' prefix, then take the
    first registration-shaped token from the remainder.
    """
    if not title:
        return None
    m = _CASE_RE.search(title)
    if not m:
        return None
    tail = title[m.end():]
    # strip leading separators / dashes / parens
    tail = tail.strip(" \t-–—:/()")
    # drop trailing parenthetical year notes like '(2025)'
    tail = re.sub(r"\(\s*\d{4}\s*\)\s*$", "", tail).strip()
    if not tail:
        return None
    # multi-reg titles use '/' or ',' — take the first
    first = re.split(r"[,/]", tail)[0].strip()
    rm = _REG_RE.search(first)
    if not rm:
        rm = _REG_RE.search(tail)
    if not rm:
        return None
    reg = rm.group(1).strip()
    # normalise internal spacing 'HI 721' → 'HI-721'
    reg = re.sub(r"\s+", "-", reg)
    return reg or None


def _parse_upload_date(block_html):
    """Parse the Phoca 'Fecha:' tooltip → ISO YYYY-MM-DD, or None.

    This is the file's UPLOAD date, used only as a weak fallback; the true
    occurrence date lives inside the PDF and is not parsed in P1.
    """
    m = _FECHA_RE.search(block_html)
    if not m:
        return None
    parts = m.group(1).split()
    if len(parts) != 3:
        return None
    try:
        day = int(parts[0])
        month = _MONTHS_ES.get(parts[1].lower())
        year = int(parts[2])
    except ValueError:
        return None
    if not month:
        return None
    try:
        import datetime
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_listing(html, page_url=""):
    """Parse a year-subcategory page → list of report dicts.

    Each dict:
      case_id            str   normalised 'CIAA-NNN-YYYY'
      pdf_url            str   absolute Phoca '?download=' URL
      report_url         str   the subcategory page URL (provenance)
      event_class        str   'Final report'|'Preliminary report'|'Provisional declaration'
      registration       str|None
      date_of_occurrence None  (occurrence date is inside the PDF; not parsed here)
      title              str   full title text
      filename           str|None  (PDF filename from the alt= attribute)

    Rows without a parseable case_id are skipped.
    """
    rows = []
    chunks = _ROW_SPLIT_RE.split(html)
    for chunk in chunks[1:]:  # chunks[0] is the pre-first-row preamble
        title_m = _TITLE_RE.search(chunk)
        dl_m = _DOWNLOAD_RE.search(chunk)
        if not title_m or not dl_m:
            continue
        title = _html.unescape(title_m.group(1)).strip()
        title = re.sub(r"\s+", " ", title)

        case_id = make_case_id(title)
        if not case_id:
            continue

        href = _html.unescape(dl_m.group(1))
        pdf_url = href if href.startswith("http") else BASE + href

        alt_m = _ALT_RE.search(chunk)
        filename = alt_m.group(1) if alt_m else None

        case_m = _CASE_RE.search(title)
        registration = _parse_registration(title, case_m.group(1) if case_m else None)

        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "report_url": page_url or None,
            "event_class": _event_class(title),
            "registration": registration,
            "date_of_occurrence": None,
            "title": title,
            "filename": filename,
        })
    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url, dest, referer=None):
    """GET the gated Phoca download URL with a Referer and write bytes to dest.

    Phoca '?download=' URLs 302/stream the actual PDF; the Referer header
    (the year-category page) matches what the site expects.  Raises on non-2xx.
    """
    headers = {"Referer": referer or REFERER}
    resp = client.get(pdf_url, headers=headers)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
