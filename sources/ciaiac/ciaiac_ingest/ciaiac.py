# ciaiac_ingest/ciaiac.py
"""CIAIAC (Spain) HTML scraper: per-year listing discovery and PDF download.

Source: https://www.transportes.gob.es/organos-colegiados/ciaiac/investigacion
- Index page lists per-year (and some per-semester) listing URLs.
- Each year page is server-rendered Drupal HTML with one <div><h2>…</h2><ul>…</ul></div>
  block per investigation row.
- PDFs are hosted on transportes.gob.es and require UA + Referer headers.
"""
import html as _html
import re
import datetime
from pathlib import Path

BASE = "https://www.transportes.gob.es"
INDEX_URL = BASE + "/organos-colegiados/ciaiac/investigacion"
REFERER = INDEX_URL
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Links to per-year (and semester) listing pages: /investigacion/YYYY or /investigacion/YYYY_Xs
# Exclude /distribucion sub-paths.
_YEAR_LINK_RE = re.compile(
    r'href="(/organos-colegiados/ciaiac/investigacion/(\d{4}(?:[_-][12]s|-(?:primer|segundo)-semestre)?))"',
    re.IGNORECASE,
)

# Row block: <div><h2>…</h2>…</div>
_ROW_BLOCK_RE = re.compile(r"<div><h2>(.*?)</h2>(.*?)</div>", re.DOTALL)

# Case reference: Ref. A-NNN/YYYY or IN-NNN/YYYY (with optional space after Ref.)
_REF_RE = re.compile(r"Ref\.?\s*((A|IN)-\d{1,4}/\d{4})", re.IGNORECASE)

# Date at start of H2: "DD de MONTH de YYYY"
_DATE_RE = re.compile(r"^(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", re.IGNORECASE)

# Registration: "matrícula XX-XXX" (EC-XXX, D-AGWD, etc.)
_LAST_REG_RE = re.compile(r"matr[íi]cula\s+(\S+?)(?=[.,\s])")

# Aircraft type: "Aeronave MODEL" or "Helicóptero MODEL" (grab first model)
_AIRCRAFT_RE = re.compile(
    r"(?:Aeron[oa]ve|Helic[oó]ptero)(?:\s+\d+:)?\s+([^,]+?)(?:,\s+matr)",
    re.IGNORECASE,
)

# PDF <li class='enlace_pdf'> blocks
_LI_PDF_RE = re.compile(r"<li class='enlace_pdf'>(.*?)</li>", re.DOTALL)
_A_HREF_RE = re.compile(r"href='(https://[^']+\.pdf)'")
_A_TITLE_RE = re.compile(r"title='([^']*)'")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_year_urls(index_html: str) -> list[str]:
    """
    Parse the CIAIAC index page → list of absolute per-year listing URLs.

    Extracts all href links matching /investigacion/YYYY (or semester variants
    like /investigacion/2002_1s, /investigacion/2019-primer-semestre) while
    excluding /distribucion sub-paths.  Preserves order, de-duplicates.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for m in _YEAR_LINK_RE.finditer(index_html):
        path = _html.unescape(m.group(1))
        # skip distribucion sub-pages
        if "/distribucion" in path:
            continue
        url = BASE + path
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ──────────────────────────────────────────────
# Row parsing helpers
# ──────────────────────────────────────────────

def _parse_date(h2_text: str) -> str | None:
    """Parse 'DD de MONTH de YYYY' → 'YYYY-MM-DD' ISO string, or None."""
    m = _DATE_RE.match(h2_text.strip())
    if not m:
        return None
    day, month_es, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTHS_ES.get(month_es)
    if not month:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_location(h2_text: str) -> str | None:
    """
    Extract location from H2 text.

    Location is the text that follows the LAST 'matrícula REG' token and
    precedes 'Ref.'.  For multi-aircraft rows (Aeronave 1 + Aeronave 2) this
    correctly returns only the location, not the second aircraft line.
    """
    # Take text before the Ref. marker
    pre_ref_m = re.match(r"^(.*?)\.\s*Ref", h2_text, re.DOTALL)
    pre_ref = pre_ref_m.group(1) if pre_ref_m else h2_text

    reg_matches = list(_LAST_REG_RE.finditer(pre_ref))
    if not reg_matches:
        return None
    after_last_reg = pre_ref[reg_matches[-1].end():].lstrip("., ")
    return after_last_reg.strip() or None


def _parse_pdfs(ul_html: str) -> tuple[str | None, str | None]:
    """
    Parse <ul class='listado_generico'> HTML.

    Returns (pdf_url_es, pdf_url_en).
    English reports are identified by 'Enlace en Inglés' in their title attr,
    or by presence of class='english' in the link body.
    Only 'Informe final' / 'Final report' PDFs are included; provisional
    declarations (dp_nm) are accepted if they're the only PDF present.
    """
    pdf_url_es: str | None = None
    pdf_url_en: str | None = None

    for li_m in _LI_PDF_RE.finditer(ul_html):
        li = li_m.group(1)
        href_m = _A_HREF_RE.search(li)
        if not href_m:
            continue
        href = href_m.group(1)
        title_m = _A_TITLE_RE.search(li)
        title = title_m.group(1) if title_m else ""
        is_en = "Inglés" in title or "english" in li

        if is_en:
            if pdf_url_en is None:
                pdf_url_en = href
        else:
            if pdf_url_es is None:
                pdf_url_es = href

    return pdf_url_es, pdf_url_en


# ──────────────────────────────────────────────
# Main listing parser
# ──────────────────────────────────────────────

def parse_listing(html: str, year_url: str = "") -> list[dict]:
    """
    Parse a per-year listing page → list of investigation dicts.

    Each dict has:
      case_id            str   e.g. 'A-005/2024' or 'IN-002/2024'
      report_url         str|None  (provisional HTML page URL if present)
      pdf_url_es         str|None  (ES final report or provisional PDF)
      pdf_url_en         str|None  (EN translation PDF if present)
      event_class        str   'Accident' | 'Serious incident'
      aircraft           str|None  (first aircraft model)
      registration       str|None  (first registration)
      date_of_occurrence str|None  ISO YYYY-MM-DD
      location           str|None
      title              str   (full H2 text)

    Rows without a parseable case_id are skipped.
    """
    rows: list[dict] = []
    for block_m in _ROW_BLOCK_RE.finditer(html):
        h2_raw = block_m.group(1)
        ul_html = block_m.group(2)
        h2 = _html.unescape(h2_raw).strip()

        # Must have a case_id
        ref_m = _REF_RE.search(h2)
        if not ref_m:
            continue
        case_id = ref_m.group(1).upper()

        # event_class
        prefix = case_id.split("-")[0]
        event_class = "Accident" if prefix == "A" else "Serious incident"

        # date
        date_iso = _parse_date(h2)

        # aircraft (first aircraft model)
        aircraft_m = _AIRCRAFT_RE.search(h2)
        aircraft = aircraft_m.group(1).strip() if aircraft_m else None

        # registration (first)
        reg_matches = list(_LAST_REG_RE.finditer(h2))
        # all registrations - pick first
        first_reg_m = re.search(r"matr[íi]cula\s+(\S+?)(?=[.,\s])", h2)
        registration = first_reg_m.group(1) if first_reg_m else None

        # location
        location = _parse_location(h2)

        # PDFs
        pdf_url_es, pdf_url_en = _parse_pdfs(ul_html)

        # report_url: the 'enlace_externo' href for provisional info page
        report_url: str | None = None
        ext_m = re.search(
            r"class='enlace_externo'.*?href='(/[^']+)'",
            ul_html,
            re.DOTALL,
        )
        if ext_m:
            report_url = BASE + _html.unescape(ext_m.group(1))

        rows.append({
            "case_id": case_id,
            "report_url": report_url,
            "pdf_url_es": pdf_url_es,
            "pdf_url_en": pdf_url_en,
            "event_class": event_class,
            "aircraft": aircraft,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": location,
            "title": h2,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with Referer header and write bytes to dest.

    The Referer header is required by CloudFront to prevent hotlinking 403s.
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
