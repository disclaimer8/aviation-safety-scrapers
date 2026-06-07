# ciaape_ingest/ciaape.py
"""CIAA Peru (Comision de Investigacion de Accidentes de Aviacion, MTC) scraper.

Source structure (live scout 2026-06-07), all on gob.pe:

  HUB / per-sheet collection (server-rendered, paginated):
    https://www.gob.pe/institucion/mtc/colecciones/383-comision-de-investigacion-de-accidentes-de-aviacion-ciaa?sheet=N
  Each sheet (N = 1..LAST, ~26 rows/sheet, ~268 results total) embeds
  anchors to individual report pages whose link TEXT carries the full
  metadata, e.g.:
    "Informe Final CIAA-ACCID-008-2022, Matricula CC-BHB, Fecha 18/11/2022"

  REPORT PAGE (one hop, to obtain the real PDF href):
    /institucion/mtc/informes-publicaciones/{id}-informe-{final|preliminar|...}-ciaa-...
  The PDF lives on cdn.www.gob.pe:
    https://cdn.www.gob.pe/uploads/document/file/{id}/{filename}.pdf

gob.pe is SLOW; use 60-90s timeouts.  Follow real hrefs at every hop;
do NOT construct cdn URLs.

case_id shapes (from the anchor/title text):
    CIAA-ACCID-NNN-YYYY   (accident)
    CIAA-INCID-NNN-YYYY   (incident)
    CIAA-SINCID-NNN-YYYY  (serious incident)
Whitespace can creep around the dashes ("CIAA- SINCID-001-2022"); it is
collapsed by _normalize_case_id.  PDFs are Spanish text-layer; narratives
are kept in Spanish (EN translation is downstream).
"""
import html as _html
import re

BASE = "https://www.gob.pe"
COLLECTION_PATH = (
    "/institucion/mtc/colecciones/"
    "383-comision-de-investigacion-de-accidentes-de-aviacion-ciaa"
)
COLLECTION_URL = BASE + COLLECTION_PATH
REFERER = BASE + "/"
DELAY = 2.0
# gob.pe is a slow state portal; default httpx timeouts time out (gov.kz lesson).
TIMEOUT = 90.0
MAX_SHEETS = 40  # safety ceiling; loop stops earlier when a sheet has 0 reports

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "es,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": REFERER,
}

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# ------------------------------------------------------------------
# Compiled regexes
# ------------------------------------------------------------------

# Anchor to an individual report page.  Capture href + inner link text.
_REPORT_ANCHOR_RE = re.compile(
    r'<a[^>]*href="(/institucion/mtc/informes-publicaciones/\d+-[a-z0-9-]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Only genuine CIAA report slugs (excludes the privacy-policy footer link, etc.)
_CIAA_SLUG_RE = re.compile(r"-ciaa-", re.IGNORECASE)

# case_id inside the link text: CIAA-{ACCID|INCID|SINCID}-NNN-YYYY (spaces tolerated)
_CASE_RE = re.compile(
    r"CIAA[\s-]*(ACCID|SINCID|INCID)[\s-]*(\d{1,4})[\s-]*(\d{4})",
    re.IGNORECASE,
)

# Registration: "Matricula REG" (OB-1332, OB-2019P, N241CH, HC-BSD, CC-BHB, HP-1844CMP, ...)
_REG_RE = re.compile(
    r"matr[ií]culas?\s+([A-Z0-9][A-Z0-9-]*[A-Z0-9])",
    re.IGNORECASE,
)

# Date: "Fecha DD/MM/YYYY"
_DATE_RE = re.compile(r"Fecha\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)

# report kind from the link text prefix
_KIND_RE = re.compile(
    r"\bInforme\s+(Final|Preliminar)\b|\bDeclaraci[oó]n\s+Provisional\b",
    re.IGNORECASE,
)

# PDF on the report page, hosted on cdn.www.gob.pe.  Exclude the preview JPG.
_PDF_HREF_RE = re.compile(
    r'(https://cdn\.www\.gob\.pe/uploads/document/file/\d+/[^"\'\s?]+\.pdf)',
    re.IGNORECASE,
)


# ------------------------------------------------------------------
# Client factory
# ------------------------------------------------------------------

def make_client():
    """Return an httpx.Client configured with browser UA and slow-portal timeout."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=TIMEOUT,
    )


def sheet_url(n: int) -> str:
    """Absolute URL for collection sheet n (1-indexed)."""
    return f"{COLLECTION_URL}?sheet={n}"


# ------------------------------------------------------------------
# case_id normalisation
# ------------------------------------------------------------------

def _normalize_case_id(raw: str) -> str:
    """Collapse stray whitespace around '-' and '/' separators and upper-case.

    "CIAA- SINCID-001-2022" -> "CIAA-SINCID-001-2022"
    "CIAA-ACCID - 008 / 2022" -> "CIAA-ACCID-008/2022"
    """
    if not raw:
        return raw
    s = _html.unescape(raw)
    s = re.sub(r"\s*([-/])\s*", r"\1", s)   # trim whitespace around - and /
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper()


def make_case_id(kind: str, number: str, year: str) -> str:
    """Build canonical CIAA case_id 'CIAA-{KIND}-{NNN}-{YYYY}'.

    kind is one of ACCID / INCID / SINCID (case-insensitive); number is
    zero-padded to 3 digits to match the site's own numbering.
    """
    k = (kind or "").upper()
    num = str(int(number)).zfill(3) if str(number).isdigit() else str(number)
    return _normalize_case_id(f"CIAA-{k}-{num}-{year}")


def _event_class(kind: str) -> str:
    k = (kind or "").upper()
    if k == "ACCID":
        return "Accident"
    if k == "SINCID":
        return "Serious incident"
    return "Incident"


# ------------------------------------------------------------------
# Discovery: parse a collection sheet -> report rows (metadata + report_url)
# ------------------------------------------------------------------

def parse_collection(html: str) -> list[dict]:
    """Parse one collection sheet -> list of report dicts.

    Each dict:
      case_id            'CIAA-ACCID-008-2022'
      report_url         absolute report-page URL (the PDF-bearing hop)
      event_class        'Accident' | 'Serious incident' | 'Incident'
      registration       str|None
      date_of_occurrence ISO YYYY-MM-DD | None
      report_type        'Informe Final' | 'Informe Preliminar' | 'Declaracion Provisional' | None
      title              full link text

    Anchors without a parseable CIAA case_id, and non-CIAA slugs (e.g. the
    privacy-policy footer), are skipped.  De-duplicates by case_id within
    the sheet (declaracion-provisional + informe-final can share a case_id;
    the first occurrence wins, and an informe-final is preferred when it
    appears).
    """
    rows: list[dict] = []
    seen: dict[str, int] = {}  # case_id -> index in rows

    for m in _REPORT_ANCHOR_RE.finditer(html):
        href = _html.unescape(m.group(1))
        if not _CIAA_SLUG_RE.search(href):
            continue
        text = re.sub(r"<[^>]+>", " ", m.group(2))
        text = _html.unescape(re.sub(r"\s+", " ", text)).strip()

        case_m = _CASE_RE.search(text) or _CASE_RE.search(href.replace("-", " "))
        if not case_m:
            continue
        case_id = make_case_id(case_m.group(1), case_m.group(2), case_m.group(3))

        reg_m = _REG_RE.search(text)
        registration = reg_m.group(1).upper() if reg_m else None

        date_iso = None
        date_m = _DATE_RE.search(text)
        if date_m:
            d, mo, y = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
            try:
                import datetime
                date_iso = datetime.date(y, mo, d).isoformat()
            except ValueError:
                date_iso = None

        kind_m = _KIND_RE.search(text)
        if kind_m:
            if kind_m.group(1):
                report_type = "Informe " + kind_m.group(1).title()
            else:
                report_type = "Declaracion Provisional"
        else:
            report_type = None

        row = {
            "case_id": case_id,
            "report_url": BASE + href,
            "event_class": _event_class(case_m.group(1)),
            "registration": registration,
            "date_of_occurrence": date_iso,
            "report_type": report_type,
            "title": text,
        }

        # de-dup within sheet; prefer an "Informe Final" over provisional
        if case_id in seen:
            idx = seen[case_id]
            existing = rows[idx]
            if (report_type or "").startswith("Informe Final") and not (
                (existing["report_type"] or "").startswith("Informe Final")
            ):
                rows[idx] = row
            continue
        seen[case_id] = len(rows)
        rows.append(row)

    return rows


# ------------------------------------------------------------------
# Report page -> PDF url
# ------------------------------------------------------------------

def parse_report_page(html: str) -> str | None:
    """Extract the report PDF URL (cdn.www.gob.pe) from a report page.

    Returns the first real .pdf cdn href (preview .jpg is excluded by the
    regex), stripped of any ?v= cache-buster, or None.
    """
    m = _PDF_HREF_RE.search(html)
    if not m:
        return None
    return m.group(1)


# ------------------------------------------------------------------
# Download
# ------------------------------------------------------------------

def download(client, pdf_url: str, dest) -> None:
    """GET pdf_url with a Referer and write bytes to dest.

    Raises httpx.HTTPStatusError on non-2xx.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
