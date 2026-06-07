# griaa_ingest/griaa.py
"""GRIAA (Colombia) HTML scraper: per-year listing discovery and PDF download.

Source: https://www.aerocivil.gov.co/investigacion/Accidentes/
Authority: Grupo de Investigación de Accidentes (GRIAA / DIACC), Aerocivil.

- The listing is static, server-rendered HTML: one <tr> per investigation with
  ten <td> columns:
      0 case_id            (COL-YY-NN-GIA / COL-YY-NN-DIACC / variants)
      1 date               (DD/MM/YYYY)
      2 event_class        (Accidente / Incidente grave)
      3 location           (municipio)
      4 operation type     (Transporte ... etc.)  -- not stored
      5 registration       (HK4235, FAC4021, N325FA, ...)
      6 aircraft type      (L-410UVP-E, TU206E, ...)
      7 category code      (RE/OTHR/...)  -- not stored
      8/9 PDF document cell(s) with <a class="document-link" href="*.pdf">
- The default page shows only the most recent rows.  A GET year filter
  ?inicio=YYYY&fin=YYYY returns that single year's rows -- this bypasses the
  filter widget's reCAPTCHA/hCaptcha entirely (the captcha only guards the POST
  form; the GET query parameters are honoured directly).
- PDFs live under /info/aeronautica_civil/media/ and need a browser UA + Referer.
- A row may carry both a "Prelim" and a "Final" PDF; we prefer the Final report.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

from .text import normalize_case_id, dmy_to_iso

BASE = "https://www.aerocivil.gov.co"
INDEX_URL = BASE + "/investigacion/Accidentes/"
REFERER = INDEX_URL
DELAY = 2.0

# Year range exposed by the listing's year <select> (1998 .. current).
YEAR_MIN = 1998
YEAR_MAX = 2026

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
# Compiled regexes
# ──────────────────────────────────────────────

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
# case_id reference: COL-YY-NN<suffix> where suffix is GIA/DIACC + optional letters
_REF_RE = re.compile(r"COL\s*-\s*\d{2}\s*-\s*\d{1,4}\s*-\s*[A-Z]+", re.IGNORECASE)
# document-link PDF anchors (href + title), grab all in a row
_PDF_A_RE = re.compile(
    r'<a[^>]*class="document-link"[^>]*href="([^"]+\.pdf)"[^>]*?(?:title="([^"]*)")?',
    re.DOTALL | re.IGNORECASE,
)
# fallback: any *.pdf href in the row
_ANY_PDF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)

_EVENT_CLASS_MAP = {
    "accidente": "Accidente",
    "incidente grave": "Incidente grave",
    "incidente": "Incidente",
}


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

def year_url(year: int) -> str:
    """Build the GET-filtered listing URL for a single year."""
    return f"{INDEX_URL}?inicio={year}&fin={year}"


def iter_year_urls(year_min: int = YEAR_MIN, year_max: int = YEAR_MAX) -> list[str]:
    """All per-year listing URLs, newest first (matches the site's order)."""
    return [year_url(y) for y in range(year_max, year_min - 1, -1)]


# ──────────────────────────────────────────────
# Row parsing helpers
# ──────────────────────────────────────────────

def _cell_text(td_html: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", td_html)).strip()


def _abs_pdf(href: str) -> str:
    """Absolutise + percent-encode a PDF href (paths contain spaces/accents)."""
    href = _html.unescape(href).strip()
    if href.startswith("http"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return BASE + urllib.parse.quote(href)


def _select_pdfs(row_html: str) -> tuple[str | None, str | None, list[str]]:
    """Return (final_pdf, prelim_pdf, all_pdfs) absolute URLs for a row.

    A row may list a preliminary and/or a final report.  Each PDF anchor is
    classified by its filename/title text:
      - 'prelim' in the name  -> preliminary report
      - 'final' in the name   -> final report (preferred)
      - otherwise             -> ambiguous; treated as a final candidate but
                                ranked below an explicitly-"final" file.
    The returned final_pdf prefers an explicitly-"final" filename over an
    ambiguously-named one; prelim_pdf is the first preliminary report.
    """
    explicit_finals: list[str] = []
    ambiguous: list[str] = []
    prelims: list[str] = []
    everything: list[str] = []

    matches = list(_PDF_A_RE.finditer(row_html))
    if matches:
        candidates = [(m.group(1), m.group(2) or "") for m in matches]
    else:
        candidates = [(m.group(1), "") for m in _ANY_PDF_RE.finditer(row_html)]

    for href, title in candidates:
        url = _abs_pdf(href)
        everything.append(url)
        blob = (href + " " + title).lower()
        if "prelim" in blob:
            prelims.append(url)
        elif "final" in blob:
            explicit_finals.append(url)
        else:
            ambiguous.append(url)

    final = (explicit_finals or ambiguous or [None])[0]
    prelim = prelims[0] if prelims else None
    return final, prelim, everything


def parse_listing(html: str, year_url: str = "") -> list[dict]:
    """Parse a (per-year) listing page → list of investigation dicts.

    Each dict has:
      case_id            str   normalised, e.g. 'COL-08-31-GIA'
      report_url         None  (no per-case HTML page; PDFs are the report)
      pdf_url_es         str|None  preferred PDF (Final > Prelim), Spanish
      pdf_url_en         None  (GRIAA reports are Spanish only)
      event_class        str   'Accidente' | 'Incidente grave' | 'Incidente'
      aircraft           str|None
      registration       str|None
      date_of_occurrence str|None  ISO YYYY-MM-DD
      location           str|None
      title              str   (first column raw)

    Rows without a parseable case_id, or with no PDF at all, are skipped
    (a report with no document cannot yield a narrative).
    """
    rows: list[dict] = []
    seen: set[str] = set()

    for row_m in _ROW_RE.finditer(html):
        row_html = row_m.group(1)
        tds = _TD_RE.findall(row_html)
        if not tds:
            continue

        # case_id is the first data cell that matches the reference pattern.
        case_id = None
        cells = [_cell_text(td) for td in tds]
        for c in cells:
            rm = _REF_RE.search(c)
            if rm:
                case_id = normalize_case_id(rm.group(0))
                break
        if not case_id or case_id in seen:
            continue

        # Locate the case_id cell index to anchor the remaining columns.
        ci = next(
            (i for i, c in enumerate(cells) if _REF_RE.search(c)), 0
        )

        def col(offset, default=None):
            idx = ci + offset
            return cells[idx] if idx < len(cells) else default

        date_raw = col(1)
        date_iso = dmy_to_iso(date_raw) if date_raw else None

        cls_raw = (col(2) or "").strip().lower()
        event_class = _EVENT_CLASS_MAP.get(cls_raw, col(2) or None)

        location = (col(3) or "").strip() or None
        registration = (col(5) or "").strip() or None
        aircraft = (col(6) or "").strip() or None

        final_pdf, prelim_pdf, all_pdfs = _select_pdfs(row_html)
        pdf_url = final_pdf or prelim_pdf
        if not pdf_url:
            # no document → cannot build a narrative; skip
            continue

        seen.add(case_id)
        rows.append({
            "case_id": case_id,
            "report_url": None,
            "pdf_url_es": pdf_url,
            "pdf_url_en": None,
            "event_class": event_class,
            "aircraft": aircraft,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": location,
            "title": cells[ci] if ci < len(cells) else case_id,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url with Referer header and write bytes to dest.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
