# dgacgt_ingest/dgacgt.py
"""Guatemala DGAC / UIA accident-report scraper.

Source: an OPEN autoindex (file listing) under
  https://www.dgac.gob.gt/wp-content/uploads/ORGANIZACION/UIA/
  INVESTIGACION DE ACCIDENTES/INFORMES FINALES/{year}/

There is no per-report metadata page: each year directory simply lists the
final-report PDFs.  Identity and metadata are therefore derived from:
  1. the PDF FILENAME  (registration + occurrence date are always present), and
  2. the PDF FIRST-PAGE header ("Reporte No.", aircraft, location), best-effort.

case_id is built from registration + ISO occurrence date (always available,
collision-safe).  The official "Reporte No." (e.g. A-02-2015, UIA-A-11-2024),
when found in the PDF text, is stored separately in `report_no`.
"""
import datetime
import html as _html
import re
from pathlib import Path
from urllib.parse import quote, unquote

BASE = "https://www.dgac.gob.gt"
INFORMES_FINALES = (
    "/wp-content/uploads/ORGANIZACION/UIA/"
    "INVESTIGACION DE ACCIDENTES/INFORMES FINALES/"
)
INDEX_URL = BASE + quote(INFORMES_FINALES)
REFERER = BASE + "/"
DELAY = 1.8

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
# Spanish month maps
# ──────────────────────────────────────────────

# 3-letter (and a few full) Spanish month abbreviations as they appear in
# filenames: ENE, FEB, MAR, ABR, MAY/MAYO, JUN, JUL, AGO, SEP, OCT, NOV, DIC.
_MONTHS_ABBR = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
# full month names (PDF header text: "31 de julio de 2024")
_MONTHS_FULL = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# ──────────────────────────────────────────────
# Regexes
# ──────────────────────────────────────────────

# Autoindex: every file/dir is an <a href="...">.  We only care about hrefs
# under the uploads tree.
_HREF_RE = re.compile(r'href="(/wp-content/uploads/[^"]+)"', re.IGNORECASE)

# A year directory link: .../INFORMES FINALES/YYYY/
_YEAR_DIR_RE = re.compile(r"/INFORMES%20FINALES/(\d{4})/?$", re.IGNORECASE)

# Registration token: TG-XXX, C6-TAK, F-GTDI, N6082P, N-431SR, N540ZA,
# YS1001P, HK-1234, etc.  Letter-prefix nationality mark, optional dash,
# then alphanumerics.  Matched against the (dash/space-normalised) filename.
_REG_RE = re.compile(
    r"\b("
    r"TG-[A-Z]{2,4}"            # Guatemala
    r"|C6-[A-Z]{2,4}"           # Bahamas
    r"|HK-[A-Z0-9]{3,5}"        # Colombia
    r"|F-[A-Z]{4}"              # France
    r"|YS-?\d{3,4}[A-Z]?"       # El Salvador (YS1001P)
    r"|YS-[A-Z]{2,4}"
    r"|N-?\d{1,5}[A-Z]{0,2}"    # USA (N6082P, N-431SR, N540ZA)
    r"|[A-Z]{1,2}-[A-Z]{3,4}"   # generic XX-XXX fallback
    r")\b"
)

# Reporte No. token inside PDF text: A-NN-YYYY or UIA-A-NN-YYYY, allowing the
# unicode hyphen U+2010 the scans sometimes use.  Captured WITHOUT a year-shape
# constraint beyond 4 trailing digits.
_REPORT_NO_RE = re.compile(
    r"\b((?:UIA[\-‐])?[A-Z]{1,3}[\-‐]\d{1,3}[\-‐]\d{4})\b"
)

# Date in filename — multiple formats, tried in order:
#   DDMONYYYY / DDMONYY      e.g. 19ENE2008, 08DIC21, 31JUL2024
#   DD MON YYYY              e.g. 25 ENE 2006, 22 MAYO 2006
#   DD-MM-YYYY / DD.MM.YYYY  e.g. 21-11-2015, 13.02.2002, 19.12.2024
#   DD-MM-YY                 e.g. 08DIC21 handled above; 06-12-24 here
_RE_DMONY = re.compile(
    r"(?<!\d)(\d{1,2})\s*([A-Za-zÁÉÍÓÚáéíóú]{3,4})\s*(\d{4}|\d{2})\b"
)
# Numeric DD<sep>MM<sep>YYYY where sep is . - / or space.  The (?<!\d) lookbehind
# stops the day from latching onto trailing registration digits (e.g. the '82'
# in 'N38782-16-10-2014').  (?!\d) after the year stops it eating into a longer
# digit run (e.g. the typo '20112').
_RE_NUM = re.compile(
    r"(?<!\d)(\d{1,2})[.\-/ ](\d{1,2})[.\-/ ](\d{4}|\d{2})(?!\d)"
)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(90.0, connect=30.0),
    )


# ──────────────────────────────────────────────
# Discovery (autoindex walk)
# ──────────────────────────────────────────────

def iter_year_urls(index_html: str) -> list[str]:
    """Parse the INFORMES FINALES autoindex -> absolute per-year dir URLs.

    De-duplicates, sorted ascending by year.
    """
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for m in _HREF_RE.finditer(index_html):
        href = m.group(1)
        ym = _YEAR_DIR_RE.search(href)
        if not ym:
            continue
        year = int(ym.group(1))
        url = BASE + href
        if not url.endswith("/"):
            url += "/"
        if url in seen:
            continue
        seen.add(url)
        out.append((year, url))
    out.sort(key=lambda t: t[0])
    return [u for _, u in out]


def iter_pdf_urls(year_html: str) -> list[str]:
    """Parse a year autoindex page -> absolute PDF URLs (case-insensitive)."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _HREF_RE.finditer(year_html):
        href = _html.unescape(m.group(1))
        if not href.lower().endswith(".pdf"):
            continue
        url = BASE + href
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ──────────────────────────────────────────────
# Filename -> registration + date
# ──────────────────────────────────────────────

def filename_from_url(pdf_url: str) -> str:
    """Return the decoded filename (no extension) from a PDF URL."""
    name = unquote(pdf_url.rsplit("/", 1)[-1])
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name


def _norm_year(yy: str) -> int:
    if len(yy) == 4:
        return int(yy)
    y = int(yy)
    return 2000 + y if (2000 + y) <= datetime.date.today().year + 1 else 1900 + y


def parse_date_from_name(name: str) -> str | None:
    """Extract an ISO YYYY-MM-DD occurrence date from a filename.

    Tries numeric DD-MM-YYYY first (least ambiguous when separators present),
    then DD<MON>YYYY / DD MON YYYY Spanish-abbreviation forms.
    """
    # 1. numeric DD-MM-YYYY / DD.MM.YYYY / DD-MM-YY
    for m in _RE_NUM.finditer(name):
        d, mo, y = int(m.group(1)), int(m.group(2)), _norm_year(m.group(3))
        if 1 <= d <= 31 and 1 <= mo <= 12:
            try:
                return datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    # 2. DD<MON>YYYY / DD MON YYYY  (Spanish abbreviation)
    for m in _RE_DMONY.finditer(name):
        d = int(m.group(1))
        mon = _strip_accents(m.group(2)).upper()[:3]
        y = _norm_year(m.group(3))
        mo = _MONTHS_ABBR.get(mon)
        if mo and 1 <= d <= 31:
            try:
                return datetime.date(y, mo, d).isoformat()
            except ValueError:
                continue
    return None


def parse_registration_from_name(name: str) -> str | None:
    """Extract the first plausible registration mark from a filename.

    The filename is dash/space-normalised so 'N-431SR' and 'N 431 SR' both
    collapse to a matchable token, but the captured value is returned with
    internal dashes preserved where present.
    """
    # Work on an upper-cased copy with NBSP / weird spaces normalised.
    up = re.sub(r"\s+", " ", name.upper())
    m = _REG_RE.search(up)
    if not m:
        return None
    reg = m.group(1)
    # Normalise N-431SR -> N431SR (the dash after a single nationality letter
    # before digits is a filename artefact, not part of the mark).
    reg = re.sub(r"^([A-Z])-(\d)", r"\1\2", reg)
    return reg


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


# ──────────────────────────────────────────────
# case_id
# ──────────────────────────────────────────────

def _normalize_case_id(raw: str) -> str:
    """Collapse whitespace around '-' and '/' and normalise unicode hyphens.

    CENIPA slug-collision lesson: differing spellings of the same id (extra
    spaces around separators) must collapse to one canonical case_id.
    """
    if not raw:
        return raw
    s = raw.replace("‐", "-").strip()
    s = re.sub(r"\s*([-/])\s*", r"\1", s)   # trim space around - and /
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-").upper()


def make_case_id(registration: str | None, date_iso: str | None,
                 year: int, fallback_name: str = "") -> str:
    """Build a collision-safe case_id.

    Preferred shape: '<REG>-<YYYY-MM-DD>'  (e.g. TG-MIC-2024-07-31).
    When registration is missing: 'DGACGT-<YYYY-MM-DD>'.
    When the date is missing too: derive a slug from the filename + year.
    """
    if registration and date_iso:
        cid = f"{registration}-{date_iso}"
    elif date_iso:
        cid = f"DGACGT-{date_iso}"
    elif registration:
        cid = f"{registration}-{year}"
    else:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", fallback_name).strip("-")
        cid = f"DGACGT-{year}-{slug}" if slug else f"DGACGT-{year}"
    return _normalize_case_id(cid)


# ──────────────────────────────────────────────
# PDF-header metadata (best-effort)
# ──────────────────────────────────────────────

def extract_report_no(text: str) -> str | None:
    """Find the official 'Reporte No.' token (A-NN-YYYY / UIA-A-NN-YYYY) in
    the first ~3000 chars of the report text."""
    head = (text or "")[:3000]
    m = _REPORT_NO_RE.search(head)
    if not m:
        return None
    return _normalize_case_id(m.group(1))


def extract_pdf_metadata(text: str) -> dict:
    """Best-effort metadata from PDF first-page text.

    Returns dict with keys aircraft, location, date_iso (any may be None).
    The DGAC report cover has fields 'Matricula', the aircraft model, the
    occurrence date in 'DD de MONTH de YYYY' form, and a location line.  The
    cover layout is column-shuffled by pdftotext, so we extract by pattern,
    not by position.
    """
    out = {"aircraft": None, "location": None, "date_iso": None}
    head = (text or "")[:2500]

    # Spanish full-date 'DD de MONTH de YYYY' or 'DD MONTH YYYY'
    dm = re.search(
        r"\b(\d{1,2})\s+(?:de\s+)?([A-Za-zÁÉÍÓÚáéíóú]{4,12})\s+(?:de\s+)?(\d{4})\b",
        head,
    )
    if dm:
        mon = _strip_accents(dm.group(2)).lower()
        mo = _MONTHS_FULL.get(mon)
        if mo:
            try:
                out["date_iso"] = datetime.date(
                    int(dm.group(3)), mo, int(dm.group(1))
                ).isoformat()
            except ValueError:
                pass

    # Location: a line mentioning departamento/municipio/Guatemala
    for line in head.splitlines():
        ls = line.strip()
        if len(ls) > 12 and re.search(
            r"\b(departamento|municipio|aeropuerto|pista|finca|Guatemala)\b",
            ls, re.IGNORECASE,
        ):
            out["location"] = re.sub(r"\s+", " ", ls).rstrip(".")
            break

    return out


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
