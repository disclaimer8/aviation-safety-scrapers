# ipiaam_ingest/ipiaam.py
"""IPIAAM (Instituto de Prevenção e Investigação de Acidentes Aeronáuticos e
Marítimos) — Cabo Verde — HTML + PDF scraper.

Source: https://www.ipiaam.cv/navsite/aeronautical-investigation-226/doc
  Static server-rendered HTML (no JS needed).  Each entry carries:
    - A direct link  https://www.ipiaam.cv/documento/opendoc/<ts>_en.pdf
    - A report reference string:  NN/INCID-A/IPIAAM/YYYY  (or NN/INCID/YYYY)
    - A publication date (not the occurrence date): YYYY-MM-DD
    - A Portuguese title

  The PDFs are all bilingual PT+EN summary reports with a structured cover
  table that supplies the OCCURRENCE date (PT: "Data / Date"), aircraft type,
  registration, operator, flight phase and injury counts.

  BAG AIA cross-check (2026-06-10): 2 CV records, both already in the main
  listing — no additional URLs needed.

case_id
-------
Built from the report reference number:
  "001/INCID-A/IPIAAM/2025" -> "ipiaam-001-incid-a-2025"
  "002/INCID/2020"          -> "ipiaam-002-incid-2020"
  "01/INCID-A/IPIAAM/2018"  -> "ipiaam-01-incid-a-2018"

When two entries share the same normalised reference (very unlikely) a
'-b', '-c' suffix is appended in listing order.

event_date
----------
The OCCURRENCE date from the PDF cover block  ("Data / Date" row), NOT the
publication date from the HTML listing.  Both PT full-word dates
("12 de maio de 2021") and DD/MM/YYYY forms are handled.  Falls back to the
HTML publication date only when the PDF cover date cannot be extracted.

Language
--------
All PDFs are bilingual PT+EN.  The full extracted text (pdftotext) contains
both languages interleaved.  The narrative_text stores the full bilingual
text; lang is set to 'en' because every report contains an English body.
(PT-only PDFs without an English section would get lang='pt', but none have
been observed.)
"""
import re

BASE = "https://www.ipiaam.cv"
LISTING_URL = BASE + "/navsite/aeronautical-investigation-226/doc"
REFERER = BASE + "/"
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ──────────────────────────────────────────────
# Regexes
# ──────────────────────────────────────────────

# PDF direct links in the listing HTML.
_PDF_LINK_RE = re.compile(
    r'https://www\.ipiaam\.cv/documento/opendoc/(\d+_en\.pdf)',
    re.IGNORECASE,
)

# Report reference strings as they appear in the HTML listing and PDF headers.
#   002/INCID-A/IPIAAM/2025
#   003/INCID-A/IPIAAM/2025
#   01/INCID-A/IPIAAM/2023
#   02/INCID-A/IPIAAM/2018
#   001/INCID-A/IPIAAM/2021
#   002/INCID/2020
_REF_RE = re.compile(
    r'\b(\d{2,3})/([A-Z][-A-Z]*(?:/[A-Z][-A-Z]*)*)/(\d{4})\b',
    re.IGNORECASE,
)

# Publication date in listing context (YYYY-MM-DD).
_PUB_DATE_RE = re.compile(r'\b(20\d{2}-\d{2}-\d{2})\b')

# Portuguese month names for DD/MM/YYYY and "DD de Mês de YYYY".
_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # English names (bilingual PDFs)
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# DD/MM/YYYY in PDF cover block.
_DMY_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b')

# "DD de Mês de YYYY" Portuguese long-form date.
_PT_LONG_DATE_RE = re.compile(
    r'\b(\d{1,2})\s+de\s+([A-Za-záàãâéêíóôõúçÁÀÃÂÉÊÍÓÔÕÚÇ]+)\s+de\s+(20\d{2})\b',
    re.IGNORECASE,
)

# PDF cover block: "Data / Date" label followed by the date value.
_DATE_LABEL_RE = re.compile(
    r'(?:Data\s*/\s*Date|Data\s+Date|Data\b)',
    re.IGNORECASE,
)

# PDF cover: "Matrícula / Registration" or plain "Matrícula" or "Registration".
_REG_LABEL_RE = re.compile(
    r'(?:Matr[ií]cula\s*/\s*Registration|Matr[ií]cula|Registration)',
    re.IGNORECASE,
)

# Registration mark: D4-XXX (Cabo Verde), or foreign (up to 8 chars).
_REG_VALUE_RE = re.compile(r'\b([A-Z0-9]{1,2}-[A-Z0-9]{2,6})\b')

# Aircraft type label.
_ACFT_LABEL_RE = re.compile(r'Tipo\s*/?\s*Type|Tipo\b', re.IGNORECASE)

# Operator label.
_OPR_LABEL_RE = re.compile(r'Operador\s*/?\s*Operator|Operador\b', re.IGNORECASE)

# Fatalities from cover block injury table.
_FATAL_RE = re.compile(
    r'(?:Fatais\s*/\s*Fatal|Fatal[s]?)\s+(\d+)\s+(\d+)\s+(\d+)',
    re.IGNORECASE,
)

# Slugify non-alphanumeric.
_NONSLUG_RE = re.compile(r'[^a-z0-9]+')

# Whitespace collapse.
_WS_RE = re.compile(r'\s+')

# HTML tags strip.
_TAG_RE = re.compile(r'<[^>]+>')


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _slugify(s: str) -> str:
    if not s:
        return ''
    return _NONSLUG_RE.sub('-', s.lower()).strip('-')


def _strip_html(s: str) -> str:
    import html as _html
    return _WS_RE.sub(' ', _html.unescape(_TAG_RE.sub(' ', s))).strip()


def make_case_id(ref: str) -> str:
    """Convert a report reference string to a stable case_id.

    Examples
    --------
    '001/INCID-A/IPIAAM/2025' -> 'ipiaam-001-incid-a-2025'
    '002/INCID/2020'          -> 'ipiaam-002-incid-2020'
    '01/INCID-A/IPIAAM/2018'  -> 'ipiaam-01-incid-a-2018'
    """
    slug = _slugify(ref)
    # Strip the 'ipiaam' segment if it appears inside the slug (redundant).
    slug = re.sub(r'-ipiaam(?=-|$)', '', slug)
    return f'ipiaam-{slug}'


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= day <= 31 and 1 <= month <= 12 and 1900 <= year <= 2100):
        return None
    return f'{year:04d}-{month:02d}-{day:02d}'


def _parse_dmy(text: str) -> str | None:
    """Parse DD/MM/YYYY form."""
    m = _DMY_RE.search(text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return _iso(d, mo, y)


def _parse_pt_long(text: str) -> str | None:
    """Parse '12 de abril de 2021' form."""
    for m in _PT_LONG_DATE_RE.finditer(text):
        mo = _PT_MONTHS.get(m.group(2).lower())
        if mo is None:
            continue
        iso = _iso(int(m.group(1)), mo, int(m.group(3)))
        if iso:
            return iso
    return None


# ──────────────────────────────────────────────
# Listing parser
# ──────────────────────────────────────────────

def parse_listing(html: str) -> list[dict]:
    """Parse the IPIAAM listing page.

    Returns a list of dicts (one per PDF), ordered as they appear on the page:
      pdf_url      str   full https://…/opendoc/…_en.pdf URL
      ts           str   timestamp token from the filename
      report_ref   str   raw reference string (e.g. '001/INCID-A/IPIAAM/2025')
      case_id      str   normalised 'ipiaam-NNN-…'
      pub_date     str   ISO publication date from listing context
      title_pt     str   Portuguese title from listing text (or empty)
    Deduplicated by case_id (first occurrence wins).
    """
    seen: set[str] = set()
    rows: list[dict] = []

    for m in _PDF_LINK_RE.finditer(html):
        ts = m.group(1).split('_')[0]
        pdf_url = m.group(0)

        # Extract context window around this PDF link to get ref/date.
        start = max(0, m.start() - 2500)
        chunk = _strip_html(html[start: m.end() + 100])

        # Report reference: pick the last one in the window (closest to URL).
        ref_match = None
        for rm in _REF_RE.finditer(chunk):
            ref_match = rm
        report_ref = (
            f'{ref_match.group(1)}/{ref_match.group(2)}/{ref_match.group(3)}'
            if ref_match else ts
        )

        case_id = make_case_id(report_ref)

        # Dedup: first occurrence of this case_id wins.
        if case_id in seen:
            continue
        seen.add(case_id)

        # Publication date.
        pub_dates = _PUB_DATE_RE.findall(chunk)
        pub_date = pub_dates[-1] if pub_dates else ''

        rows.append({
            'pdf_url': pdf_url,
            'ts': ts,
            'report_ref': report_ref,
            'case_id': case_id,
            'pub_date': pub_date,
            'title_pt': '',  # enriched by build step from PDF text
        })

    return rows


# ──────────────────────────────────────────────
# PDF text extraction helpers
# ──────────────────────────────────────────────

_HEAD_CHARS = 3000


def extract_event_date(text: str) -> str | None:
    """Extract OCCURRENCE date from PDF cover block.

    Strategy:
    1. Find 'Data / Date' label, then parse DD/MM/YYYY or PT-long date in
       the following 200 chars.
    2. Fall back to the first DD/MM/YYYY in the first 3000 chars of text.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]

    # 1. Label-anchored.
    lm = _DATE_LABEL_RE.search(head)
    if lm:
        window = head[lm.end(): lm.end() + 200]
        iso = _parse_dmy(window) or _parse_pt_long(window)
        if iso:
            return iso

    # 2. Fallback: first DD/MM/YYYY anywhere in head.
    return _parse_dmy(head)


def extract_registration(text: str) -> str | None:
    """Extract the primary aircraft registration from the PDF cover block.

    Tries in order:
    1. Labelled 'Matrícula / Registration' row in cover table.
    2. 'registered <REG>' phrasing in the first paragraph (English-style reports).
    3. First D4-XXX (Cabo Verde registration) anywhere in the head.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    lm = _REG_LABEL_RE.search(head)
    if lm:
        window = head[lm.end(): lm.end() + 80]
        rm = _REG_VALUE_RE.search(window)
        if rm:
            return rm.group(1).upper()
    # Fallback 1: "registered <REG>" / "registration <REG>" phrase.
    m = re.search(
        r'(?:registered|registration)\s+([A-Z0-9]{1,2}-[A-Z0-9]{2,6})\b',
        head, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()
    # Fallback 2: first D4-XXX (Cabo Verde reg) in head.
    m = re.search(r'\bD4\s*[-–]\s*[A-Z]{2,3}\b', head)
    if m:
        return re.sub(r'\s*[-–]\s*', '-', m.group(0)).upper()
    return None


def extract_report_ref_from_pdf(text: str) -> str | None:
    """Extract the IPIAAM report reference from PDF text body.

    Matches patterns like:
      001/INCID-A/IPIAAM/2025
      001/INCID-A/2025
      001/INCID/2020
      02/INCID-A/IPIAAM/2018
    Returns the raw string or None.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    # Broader pattern: NN(N)/INCID-A(.../IPIAAM)?/YYYY  or  NN/INCID/YYYY
    m = re.search(
        r'\b(\d{2,3}/(?:INCID-A(?:/IPIAAM)?|INCID)/20\d{2})\b',
        head,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def extract_aircraft(text: str) -> str | None:
    """Extract aircraft type from PDF cover block."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    lm = _ACFT_LABEL_RE.search(head)
    if not lm:
        return None
    window = head[lm.end(): lm.end() + 120]
    val = re.match(r'[\s\-:/]*([^\n]{3,50})', window)
    if not val:
        return None
    v = _WS_RE.sub(' ', val.group(1)).strip(' \t\r\n-:/|')
    # Drop if it looks like a label continuation.
    if re.match(r'(?:N[oº]|Nº|Serie|Série|Matrícula|Registration)\b', v, re.I):
        return None
    return v or None


def extract_operator(text: str) -> str | None:
    """Extract operator from PDF cover block."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    lm = _OPR_LABEL_RE.search(head)
    if not lm:
        return None
    window = head[lm.end(): lm.end() + 120]
    val = re.match(r'[\s\-:/]*([^\n]{3,80})', window)
    if not val:
        return None
    return _WS_RE.sub(' ', val.group(1)).strip(' -:/|') or None


def extract_fatalities(text: str) -> int | None:
    """Sum fatal injuries (crew + pax + others) from the PDF cover block."""
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = _FATAL_RE.search(head)
    if not m:
        return None
    total = int(m.group(1)) + int(m.group(2)) + int(m.group(3))
    return total


def detect_lang(text: str) -> str:
    """Return 'en' if an English section is present, else 'pt'."""
    if not text:
        return 'pt'
    # EN presence heuristic: look for common English phrases in the first 4K.
    sample = text[:4000]
    en_hits = len(re.findall(
        r'\b(?:Summary|Report|Investigation|Occurrence|Aircraft|Registration|'
        r'Operator|Serious\s+Incident|Type\s+of\s+Event|History\s+of\s+flight)\b',
        sample, re.IGNORECASE,
    ))
    return 'en' if en_hits >= 2 else 'pt'


def make_site_slug(aircraft: str | None, registration: str | None, location: str | None) -> str:
    parts = [p for p in (aircraft, registration, location) if p]
    base = _slugify(' '.join(parts))
    return f'crash-{base}' if base else 'crash-ipiaam'
