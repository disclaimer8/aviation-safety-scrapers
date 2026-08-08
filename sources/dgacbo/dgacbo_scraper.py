#!/usr/bin/env python3
"""DGACBO (Bolivia — Dirección General de Aeronáutica Civil, AIG) aviation-accident ingest.

Source: www.dgac.gob.bo official investigation reports.
TWO URL patterns:
  OLD (2017-2019): wp-content/aig/{YEAR}/CP_{reg}.pdf or CP_{reg}_ACCID_{N}_{YY}.pdf
    ⚠️ aig/ DIRECTORY returns 403; individual files return 200.
    Enumerate via Wayback CDX wildcard: dgac.gob.bo/wp-content/aig/*
  NEW (2021-2023): wp-content/uploads/{YEAR}/{MM}/INFORME-FINAL-ACCID-{N}-{YY}-CP-{reg}.pdf
    Enumerate via WP media API:
      https://www.dgac.gob.bo/wp-json/wp/v2/media?mime_type=application/pdf&search=ACCID
      https://www.dgac.gob.bo/wp-json/wp/v2/media?mime_type=application/pdf&search=INFORME-FINAL-ACCID
    ⚠️ DO NOT use aig.dgac.gob.bo (Angular SPA) or morvor.dgac.gob.bo (403).

case_id: 'dgacbo-accid-NN-YY' from intrinsic ACCID code in PDF text.
  Fallback: 'dgacbo-cp-NNNN-YYYY' from registration + event year.
  INC (incidents) get 'dgacbo-inc-NN-YY'.

event_date: extracted from Spanish PDF text (NOT from URL path / upload year).
  Patterns: '24 DE MARZO DE 2019', 'Fecha: 24-03-2019', ISO dates.
  Old-style scanned PDFs (2017-2018) need OCR — activate via OCR_REMOTE.

Bolivian civil registrations: CP-NNNN (4-digit number).

report_type: 'Preliminary report' if DECLARACION or PRELIMINAR in text/filename.
Supersession: same ACCID number → keep only most recent (Final > Preliminary).

Stages: discover | fetch | parse | parse-skipped | build | recheck | stats
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile, uuid, urllib.request

DGACBO_BASE = "https://www.dgac.gob.bo"
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"
WP_API_BASE = "https://www.dgac.gob.bo/wp-json/wp/v2/media"

DELAY = 1.5       # base inter-request delay
FLOOR = 300       # minimum chars to consider text usable
HOME = os.path.expanduser("~/dgacbo-ingest")
DB = os.path.join(HOME, "dgacbo.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# CDX years for the old aig/ path pattern
CDX_AIG_YEARS = [2017, 2018, 2019]

OCR_LANG = "spa"

# ---- SCHEMA ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS dgacbo_reports (
  case_id       TEXT PRIMARY KEY,
  source_url    TEXT,
  archive_url   TEXT,
  archive_ts    TEXT,
  pdf_path      TEXT,
  report_type   TEXT,
  aircraft      TEXT,
  registration  TEXT,
  event_date    TEXT,
  location      TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  operator      TEXT,
  lang          TEXT DEFAULT 'es',
  status        TEXT DEFAULT 'new',
  skip_reason   TEXT,
  discovered_at INT,
  updated_at    INT
);
CREATE TABLE IF NOT EXISTS dgacbo_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'BO',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'es',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_dgacbo_status ON dgacbo_reports(status);
CREATE INDEX IF NOT EXISTS idx_dgacbo_source_url ON dgacbo_reports(source_url);
"""

# ---- DATE PARSING (Spanish) -------------------------------------------------

ES_MONTHS = {
    "enero": 1, "ene": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "mayo": 5,
    "junio": 6, "jun": 6,
    "julio": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "sep": 9, "sept": 9,
    "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12,
}
_ES_MONTH_ALT = "|".join(sorted(ES_MONTHS, key=len, reverse=True))

# "24 DE MARZO DE 2019", "1 DE SEPTIEMBRE DE 2021"
_ES_DATE_PAT = re.compile(
    r'\b(\d{1,2})\s+(?:de\s+)?(' + _ES_MONTH_ALT + r')(?:\s+de(?:l)?)?[\s,]+(\d{4})\b',
    re.IGNORECASE,
)
# "MARZO 24, 2019"  (month first, rare)
_ES_DATE_MF = re.compile(
    r'\b(' + _ES_MONTH_ALT + r')\s+(\d{1,2})[,\s]+(?:de\s+)?(\d{4})\b',
    re.IGNORECASE,
)
# ISO / numeric: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
_ISO_DATE = re.compile(
    r'\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b'
    r'|'
    r'\b([0-3]?\d)[.\-/]([01]?\d)[.\-/]((?:19|20)\d{2})\b'
)
# "Fecha: 24-03-2019" or "FECHA Y HORA DEL\n13/ 06/ 2018" field label
# Captures date that may appear on same line OR next line, with optional spaces
_FECHA_PAT = re.compile(
    r'(?:FECHA(?:\s+Y\s+HORA)?(?:\s+DEL?)?(?:\s+(?:Suceso|Accidente|Incidente|Evento))?)[:\s]+([0-9\s\-/\.]+)',
    re.IGNORECASE,
)
# Numeric date with possible spaces: "13/ 06/ 2018" or "13/06/2018"
_NUM_DATE_SPACED = re.compile(
    r'(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*((?:19|20)\d{2})'
)


def _es_month(s):
    s = s.lower()
    for k in sorted(ES_MONTHS, key=len, reverse=True):
        if s.startswith(k):
            return ES_MONTHS[k]
    return None


def _try_num_date(raw):
    """Try to parse numeric date from raw string (may have spaces). Returns (y,mo,d) or None."""
    raw = raw.strip()
    # DD/ MM/ YYYY with spaces
    m = _NUM_DATE_SPACED.match(raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2030:
            return y, mo, d
    # DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY (no spaces)
    m = re.match(r'(\d{1,2})[.\-/](\d{1,2})[.\-/]((?:19|20)\d{2})', raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2030:
            return y, mo, d
    return None


def parse_date(txt, filename=None):
    """Parse event date from Spanish PDF text. Returns ISO YYYY-MM-DD or None.

    Bolivia reports embed the accident date in structured fields, NOT in narrative prose.
    A date appearing near "Investigadores" or "Ciudad, DD de Mes de YYYY" is the
    SIGNING date of the report — must be rejected.

    Priority:
    1. FECHA field label (Bolivia tabular format) — first 5000 chars
    2. Numeric date with spaces (DD/ MM/ YYYY) near FECHA context
    3. "Fecha" label + numeric date elsewhere in first 2000 chars
    4. ISO numeric date in first 2000 chars (DD-MM-YY two-digit year)
    5. Spanish prose date — ONLY if not preceded by signing-context words
    ⚠️ NEVER use URL upload-year as event date.
    """
    if txt:
        header = txt[:5000]

        # 1. FECHA field label (Bolivia format: "FECHA Y HORA DEL\n13/ 06/ 2018")
        m = _FECHA_PAT.search(header)
        if m:
            raw = m.group(1).strip()
            result = _try_num_date(raw[:20])
            if not result:
                for line in raw.splitlines():
                    result = _try_num_date(line.strip()[:20])
                    if result:
                        break
            if result:
                y, mo, d = result
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 2. Numeric DD/ MM/ YYYY with spaces in first 5000 chars
        m = _NUM_DATE_SPACED.search(header)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 3. Near 'fecha' keyword with numeric date (DD-MM-YY format)
        # "fecha 03-04-18 a horas" → 2018-04-03
        m = re.search(r'[Ff]echa\s+(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', header)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), _norm_year(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31 and 2010 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 4. ISO YYYY-MM-DD in first 3000 chars
        short = txt[:3000]
        m = re.search(r'\b(20\d{2})[.\-/]([01]\d)[.\-/]([0-3]\d)\b', short)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"

        # 5. Spanish prose date — search first 8000 chars, but skip signing context
        # Signing context: "Ciudad, DD de Mes de YYYY\nInvestigadores"
        _SIGNING_CTX = re.compile(
            r'(?:Investigador|Santa Cruz,|La Paz,|Cochabamba,|Trinidad,|Oruro,|Potosí,)',
            re.IGNORECASE,
        )
        for m in _ES_DATE_PAT.finditer(txt[:8000]):
            d, month_str, y = int(m.group(1)), m.group(2), int(m.group(3))
            mo = _es_month(month_str)
            if not (mo and 1 <= d <= 31 and 2000 <= y <= 2030):
                continue
            # Check if this match is preceded by signing context within 200 chars
            ctx_before = txt[max(0, m.start()-200):m.start()]
            if _SIGNING_CTX.search(ctx_before):
                continue  # skip — this is the report signing date
            return f"{y:04d}-{mo:02d}-{d:02d}"

        # 5b. month-first prose (last resort)
        for m in _ES_DATE_MF.finditer(txt[:8000]):
            month_str, d, y = m.group(1), int(m.group(2)), int(m.group(3))
            mo = _es_month(month_str)
            if not (mo and 1 <= d <= 31 and 2000 <= y <= 2030):
                continue
            ctx_before = txt[max(0, m.start()-200):m.start()]
            if _SIGNING_CTX.search(ctx_before):
                continue
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


# ---- METADATA EXTRACTION ----------------------------------------------------

# Bolivia civil reg: CP-NNNN (4 digits), military TAM-XXX or FAB-XXX
_REG_BO = re.compile(r'\b(CP-\d{3,5}|TAM-[A-Z0-9]{2,6}|FAB-[A-Z0-9]{2,6})\b')
# Fallback: generic ICAO-style reg near label
_REG_LABEL = re.compile(
    r'(?:Matr[íi]cula|Registro|N[°º\.]\s*de\s+Aeronave)[:\s]+([A-Z]{1,3}-[A-Z0-9]{2,6})',
    re.IGNORECASE,
)

# ACCID/INC code in text:
#   New format: "ACCID-05-21", "ACCIDENTE-03-2019", "ACCID-05/18" (slash variant)
#   Old format: "ACC-09-17", "ACC-08-17"
_ACCID_CODE = re.compile(
    r'(?:C[óo]digo[:\s]+)?(?:ACCID(?:ENTE)?|ACC)[\s\-]+(\d{1,3})[\s\-/]+(\d{2,4})',
    re.IGNORECASE,
)
# INC code in text: "INC-01-19", "INCIDENTE-01-2019", "INCIDENTE GRAVE -1-19", "INCID GRAV-07-17"
_INC_CODE = re.compile(
    r'(?:C[óo]digo[:\s]+)?(?:INC(?:IDENTE)?(?:\s+GRAVE)?|INCID(?:\s+GRAV)?)[\s\-]+(\d{1,3})[\s\-/]+(\d{2,4})',
    re.IGNORECASE,
)
# INCD/GRAV code: "INCD. GRAV-01-18"
_INCD_CODE = re.compile(
    r'INCD[\.\s]+GRAV[\s\-]+(\d{1,3})[\s\-/]+(\d{2,4})',
    re.IGNORECASE,
)
# ACCID from filename: ACCID_09_18, ACCID-09-21, ACCID_03_19
_ACCID_FN = re.compile(r'ACCID[_\-](\d{1,3})[_\-](\d{2})', re.IGNORECASE)
_INC_FN = re.compile(r'INC[_\-](\d{1,3})[_\-](\d{2})', re.IGNORECASE)


def _norm_year(yy_or_yyyy):
    """Normalize 2-digit year to 4-digit (2000-based for aviation reports)."""
    s = str(int(yy_or_yyyy))
    if len(s) <= 2:
        n = int(s)
        return 2000 + n if n <= 99 else n
    return int(s)


def parse_accid_code(txt, filename=None):
    """Extract (kind, num, year) from PDF text or filename.
    kind = 'accid' or 'inc'.
    Returns (kind, norm_num, norm_year) or None.
    """
    if txt:
        header = txt[:3000]
        m = _ACCID_CODE.search(header)
        if m:
            num, yr = int(m.group(1)), _norm_year(m.group(2))
            if 1 <= num <= 99 and 2010 <= yr <= 2030:
                return ('accid', num, yr)
        m = _INC_CODE.search(header)
        if m:
            num, yr = int(m.group(1)), _norm_year(m.group(2))
            if 1 <= num <= 99 and 2010 <= yr <= 2030:
                return ('inc', num, yr)
        m = _INCD_CODE.search(header)
        if m:
            num, yr = int(m.group(1)), _norm_year(m.group(2))
            if 1 <= num <= 99 and 2010 <= yr <= 2030:
                return ('inc', num, yr)
    if filename:
        fn = urllib.parse.unquote(filename).split('/')[-1]
        m = _ACCID_FN.search(fn)
        if m:
            num, yr = int(m.group(1)), _norm_year(m.group(2))
            if 1 <= num <= 99 and 2010 <= yr <= 2030:
                return ('accid', num, yr)
        m = _INC_FN.search(fn)
        if m:
            num, yr = int(m.group(1)), _norm_year(m.group(2))
            if 1 <= num <= 99 and 2010 <= yr <= 2030:
                return ('inc', num, yr)
    return None


def parse_registration(txt):
    """Extract aircraft registration."""
    if not txt:
        return None
    header = txt[:4000]
    # 1. CP-NNNN (Bolivia civil)
    m = re.search(r'\b(CP-\d{3,5})\b', header)
    if m:
        return m.group(1)
    # 2. Military
    m = _REG_BO.search(header)
    if m:
        return m.group(1)
    # 3. Near a label
    m = _REG_LABEL.search(header)
    if m:
        return m.group(1)
    return None


def parse_aircraft(txt):
    """Extract aircraft type/model from Bolivia report format.

    Bolivia format: "FABRICANTE/MODELO/MSN: CESSNA / 210L / 21059960"
    or "MARCA Y MODELO: CESSNA TU 206 G"
    """
    if not txt:
        return None
    header = txt[:5000]
    # Bolivia field "FABRICANTE/MODELO/MSN: MAKE / MODEL / SN"
    # Pattern may span two lines: "FABRICANTE / MODELO / MSN:\nCESSNA / 210L / 21059960"
    # Capture the line AFTER the label if value is on next line
    m = re.search(
        r'(?:FABRICANTE\s*/\s*MODELO(?:\s*/\s*MSN)?)[:\s]*\n?([A-Za-z][A-Za-z0-9\s\-/\.]{1,80})',
        header, re.IGNORECASE
    )
    if m:
        full = re.split(r'[\n\r]', m.group(1))[0].strip()
        # Skip if captured value is itself a label phrase
        if re.match(r'(?:Marca|Matricula|Explotador|Operador|Propietario|Base|Lugar)', full, re.I):
            # Try to find aircraft name differently — scan for "MAKE / MODEL" pattern in text
            m2 = re.search(
                r'((?:CESSNA|PIPER|BELL|BEECHCRAFT|BEECH|DHC|ATR)\s*/\s*[\w\-\.]+(?:\s*/\s*[\w\-\.]+)?)',
                header, re.IGNORECASE
            )
            if m2:
                full = m2.group(1)
            else:
                full = None
        if full:
            full_clean = _clean_aircraft(re.split(r'[\n\r]', full)[0].strip())
            # Skip if it looks like a person's name (has 2+ title-case words, no digits)
            if re.match(r'[A-ZÁÉÍÓÚ][a-záéíóú]+\s+[A-ZÁÉÍÓÚ]', full_clean) and not re.search(r'\d', full_clean):
                full = None  # person name, not aircraft
        if full:
            # "CESSNA / 210L / 21059960" → take first two slash-parts
            parts = [p.strip() for p in full.split('/')]
            if len(parts) >= 2:
                make_model = f"{parts[0]} {parts[1]}".strip()
                make_model = re.sub(r'\s+', ' ', make_model).strip(',;.')
                v = _clean_aircraft(make_model)
                # Also clean trailing MSN/serial (numeric string possibly with dashes)
                v = re.sub(r'\s+[\d][\d\-]{4,}$', '', v).strip()
                if 2 < len(v) < 50 and not re.match(r'[A-Z][a-z]+\s+[A-Z][a-z]+', v):
                    return v
            elif parts:
                v = _clean_aircraft(parts[0].strip(',;.'))
                if 2 < len(v) < 50:
                    return v
    # "MARCA Y MODELO: CESSNA TU 206 G"
    m = re.search(
        r'(?:MARCA Y MODELO|Marca y Modelo)[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-\.]{2,40})',
        header, re.IGNORECASE
    )
    if m:
        v = _clean_aircraft(re.split(r'[\n\r]', m.group(1))[0].strip())
        if 2 < len(v) < 50:
            return v
    # Declaracion Provisional tabular format:
    # "DATOS SOBRE LA AERONAVE\nMARCA\nMODELO\nANO\nMAKE_VALUE\nMODEL_VALUE\nYEAR\n"
    # All three headers appear first as column labels, then the values follow
    m = re.search(
        r'DATOS SOBRE LA AERONAVE\s*\n'
        r'MARCA\s*\n'
        r'(?:MODELO\s*\n)?'
        r'(?:A[^\n]{0,5}\s*\n)?'
        r'([A-Za-z][A-Za-z0-9\.\s]{2,40})\s*\n'
        r'([A-Za-z0-9][\w\-\.]{1,20})',
        header, re.IGNORECASE
    )
    if m:
        make = m.group(1).strip()
        model = m.group(2).strip()
        v = _clean_aircraft(f"{make} {model}")
        if 2 < len(v) < 60:
            return v
    # Fallback: known make names — look for isolated make word
    m = re.search(
        r'\b((?:CESSNA|PIPER|BELL|BEECHCRAFT|BEECH|DHC|ATR|PILATUS|SOCATA|ROBINSON|EUROCOPTER|SIKORSKY)[\w\s\-\.]{1,30})',
        header, re.IGNORECASE
    )
    if m:
        v = _clean_aircraft(re.split(r'[\n\r]', m.group(1))[0].strip())
        if 2 < len(v) < 50:
            # Reject if it's a label phrase
            if not re.search(r'(?:Marca|nacionalidad|registr|certific)', v, re.I):
                return v
    return None


def _clean_aircraft(v):
    """Post-process extracted aircraft string."""
    if not v:
        return v
    # Strip leading single-letter OCR artifact: "I CESSNA" → "CESSNA", "i PIPER" → "PIPER"
    v = re.sub(r'^[Ii\|]\s+', '', v).strip()
    # Strip trailing serial/reg that bled in
    v = re.sub(r'\s+(?:CP|HC|N)-\d+.*$', '', v, flags=re.I).strip()
    # Strip MSN-like trailing number: "CESSNA 210L 21059960" → if last part is long number
    v = re.sub(r'\s+\d{6,}$', '', v).strip()
    return v.strip(',;.').strip()


def parse_location(txt):
    """Extract accident location."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r'(?:LUGAR DEL (ACCIDENTE|INCIDENTE|SUCESO)|Lugar del (Accidente|Incidente|Suceso))[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][^\n]{3,80})',
        r'(?:LUGAR|Lugar)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][^\n]{3,80})',
        r'(?:Lugar del Accidente\s*\n)([A-Za-zÁÉÍÓÚáéíóúÑñ][^\n]{3,80})',
        r'(?:DEPARTAMENTO|Departamento)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][^\n]{3,60})',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            # For groups with alternatives, get last non-None group
            grps = [g for g in m.groups() if g is not None]
            if grps:
                v = grps[-1].strip().strip(',;.')
                if 3 < len(v) < 100:
                    return v
    return None


def parse_operator(txt):
    """Extract operator."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r'(?:OPERADOR|Operador)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ0-9][^\n]{3,80})',
        r'(?:PROPIETARIO|Propietario)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ0-9][^\n]{3,80})',
        r'(?:EXPLOTADOR|Explotador)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ0-9][^\n]{3,80})',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            if 2 < len(v) < 120:
                return v
    return None


def parse_probable_cause(txt):
    """Extract probable cause / conclusions section."""
    if not txt:
        return None
    for section_pat in [
        r'(?:CAUSAS?\s+PROBABLE[S]?|CAUSA\s+PROBABLE)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:CAUSA(?:S)?\s+DEL\s+ACCIDENTE)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:FACTORES\s+CONTRIBUYENTES?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:CONCLUSI[OÓ]NES?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:Causa(?:s)? del [Aa]ccidente)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:Causa(?:s)? [Pp]robable(?:s)?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
    ]:
        m = re.search(section_pat, txt, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1).strip()[:2500]
            if len(section) > 40:
                return section
    return None


def parse_report_type(txt, filename=None):
    """Classify as 'Final report' or 'Preliminary report'."""
    sources = []
    if txt:
        sources.append(txt[:2000].upper())
    if filename:
        sources.append(filename.upper())
    for s in sources:
        if re.search(r'PRELIMINAR|PREVIO|PROVISIONAL|PRELIMINARY', s):
            return "Preliminary report"
    return "Final report"


# ---- CASE-ID CONSTRUCTION ---------------------------------------------------

def build_case_id(accid_code, registration, event_date, source_url):
    """Build stable intrinsic case_id.

    Priority:
    1. ACCID code from text/filename: (accid,5,21) → 'dgacbo-accid-05-21'
    2. registration + event_year: 'dgacbo-cp-3104-2021'
    3. URL-derived slug (fallback)
    """
    if accid_code:
        kind, num, yr = accid_code
        return f"dgacbo-{kind}-{num:02d}-{str(yr)[-2:]}"

    if registration and event_date:
        reg_slug = re.sub(r'[^a-z0-9]+', '-', registration.lower()).strip('-')
        yr = event_date[:4]
        return f"dgacbo-{reg_slug}-{yr}"

    fn = urllib.parse.unquote(source_url).split('/')[-1]
    fn = re.sub(r'\.(?:pdf|PDF)$', '', fn)
    slug = re.sub(r'[^a-z0-9]+', '-', fn.lower()).strip('-')[:80]
    return f"dgacbo-{slug}"


# ---- OCR HELPERS (mirrors dgacec-ingest) ------------------------------------

def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote host. Returns '' on failure."""
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    """OCR a scanned PDF via OCR_REMOTE (hetzner) or local ocrmypdf."""
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                ["ocrmypdf", "--force-ocr", "--language", lang,
                 "--sidecar", sidecar, "--output-type", "none",
                 str(pdf_path), "-"],
                capture_output=True, timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    finally:
        try:
            os.unlink(sidecar)
        except OSError:
            pass


# ---- HTTP HELPERS -----------------------------------------------------------

def now():
    return int(time.time() * 1000)


def http_get(url, retries=3, timeout_sec=20):
    """GET with exponential backoff on 429/503."""
    delay = DELAY
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                content = resp.read()
            time.sleep(delay)
            return content, 200
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] HTTP {e.code} → sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            print(f"  [http] {e.code} for {url}", file=sys.stderr)
            return None, e.code
        except Exception as e:
            if attempt < retries - 1:
                wait = 8 * (attempt + 1)
                print(f"  [http error] {type(e).__name__}: {e} → sleep {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  [http fatal] {type(e).__name__}: {e}", file=sys.stderr)
    return None, 0


def curl_download(url, dest, max_time=60):
    """Download url to dest via curl. Returns True on success."""
    r = subprocess.run(
        ["curl", "-sL", "--max-time", str(max_time), "--connect-timeout", "8",
         "-A", UA, "-o", dest, url],
        capture_output=True, timeout=max_time + 10,
    )
    if r.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 100:
        return False
    with open(dest, 'rb') as f:
        magic = f.read(4)
    if magic != b'%PDF':
        try:
            os.unlink(dest)
        except OSError:
            pass
        return False
    return True


def cdx_best_snapshot(orig_url):
    """Find most recent CDX 200-snapshot timestamp for a URL."""
    url = (
        f"{CDX_BASE}?url={urllib.parse.quote(orig_url, safe=':/')}"
        f"&output=json&filter=statuscode:200&limit=5&fl=timestamp"
    )
    content, status = http_get(url)
    if not content or status != 200:
        return None
    try:
        data = json.loads(content)
        rows = data[1:] if data else []
        return rows[-1][0] if rows else None
    except Exception:
        return None


# ---- DB ---------------------------------------------------------------------

def conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def extract_text(path):
    """Run pdftotext on a downloaded PDF."""
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(path), "-"],
            capture_output=True, timeout=180,
        )
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


# ---- DISCOVER ---------------------------------------------------------------

def discover(c):
    """Enumerate Bolivia DGAC accident report PDFs from three sources.

    Method A: WP media API search=ACCID + search=INFORME-FINAL-ACCID (paginated)
    Method B: Wayback CDX wildcard for wp-content/aig/YEAR/* (2017-2019)
    Union + dedup by URL.
    """
    seen_urls = {row["source_url"] for row in c.execute("SELECT source_url FROM dgacbo_reports")}
    inserted = 0
    stats = {"wp_api": 0, "cdx_aig": 0, "skipped_nonaccid": 0}

    # --- Method A: WP media API ---
    for search_term in ["ACCID", "INFORME-FINAL-ACCID"]:
        page = 1
        while True:
            url = (f"{WP_API_BASE}?mime_type=application/pdf"
                   f"&search={urllib.parse.quote(search_term)}"
                   f"&per_page=100&page={page}")
            print(f"[dgacbo discover] WP API search={search_term} page={page}...", flush=True)
            content, status = http_get(url)
            if not content or status not in (200, 400):
                break
            if status == 400:
                break
            try:
                items = json.loads(content)
            except Exception:
                break
            if not items:
                break
            print(f"  → {len(items)} items", flush=True)
            for item in items:
                pdf_url = (item.get("guid", {}).get("rendered", "")
                           or item.get("source_url", ""))
                if not pdf_url or not pdf_url.lower().endswith(".pdf"):
                    continue
                # Filter: must look like an accident report
                fn = pdf_url.split("/")[-1]
                if not re.search(r'ACCID|INC[_\-]', fn, re.I):
                    stats["skipped_nonaccid"] += 1
                    continue
                # Skip non-investigation docs
                if re.search(r'PROTOCOLO|PROCEDIMIENTO|ESTADIST|EVALUACION|POA|CIRCUNSTANCIADO|GESTIO|PRESUPUEST', fn, re.I):
                    stats["skipped_nonaccid"] += 1
                    continue
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                prelim_cid = _url_slug_cid(pdf_url)
                prelim_cid = _dedup_cid(c, prelim_cid)
                c.execute(
                    "INSERT OR IGNORE INTO dgacbo_reports "
                    "(case_id, source_url, lang, status, discovered_at, updated_at) "
                    "VALUES (?, ?, 'es', 'new', ?, ?)",
                    (prelim_cid, pdf_url, now(), now()),
                )
                c.commit()
                inserted += 1
                stats["wp_api"] += 1
            if len(items) < 100:
                break
            page += 1
            time.sleep(DELAY)

    # --- Method B: Wayback CDX for aig/ path ---
    for year in CDX_AIG_YEARS:
        cdx_url = (
            f"{CDX_BASE}?url=www.dgac.gob.bo/wp-content/aig/{year}/*"
            f"&output=json&filter=mimetype:application/pdf&filter=statuscode:200"
            f"&fl=original,timestamp&collapse=urlkey&limit=500"
        )
        print(f"[dgacbo discover] CDX aig/{year}...", flush=True)
        content, status = http_get(cdx_url, timeout_sec=30)
        if not content or status != 200:
            print(f"  CDX aig/{year}: failed status={status}", flush=True)
            continue
        try:
            data = json.loads(content)
            rows = data[1:] if data else []
        except Exception:
            rows = []
        print(f"  → {len(rows)} CDX rows", flush=True)
        for row in rows:
            orig_url = row[0]
            # Normalize case in extension (.PDF → .pdf)
            orig_url_norm = re.sub(r'\.PDF$', '.pdf', orig_url, flags=re.I)
            if orig_url_norm in seen_urls or orig_url in seen_urls:
                continue
            seen_urls.add(orig_url_norm)
            prelim_cid = _url_slug_cid(orig_url_norm)
            prelim_cid = _dedup_cid(c, prelim_cid)
            c.execute(
                "INSERT OR IGNORE INTO dgacbo_reports "
                "(case_id, source_url, lang, status, discovered_at, updated_at) "
                "VALUES (?, ?, 'es', 'new', ?, ?)",
                (prelim_cid, orig_url_norm, now(), now()),
            )
            c.commit()
            inserted += 1
            stats["cdx_aig"] += 1
        time.sleep(DELAY)

    print(
        f"[dgacbo discover] inserted={inserted} "
        f"wp_api={stats['wp_api']} cdx_aig={stats['cdx_aig']} "
        f"skipped_nonaccid={stats['skipped_nonaccid']}",
        flush=True,
    )
    return inserted


def _url_slug_cid(url):
    fn = urllib.parse.unquote(url).split('/')[-1]
    fn = re.sub(r'\.(?:pdf|PDF)$', '', fn, flags=re.I)
    slug = re.sub(r'[^a-z0-9]+', '-', fn.lower()).strip('-')[:80]
    return f"dgacbo-{slug}"


def _dedup_cid(c, base):
    """Return unique case_id (add -2, -3 suffix if needed)."""
    cid = base
    n = 1
    while c.execute("SELECT 1 FROM dgacbo_reports WHERE case_id=?", (cid,)).fetchone():
        n += 1
        cid = f"{base}-{n}"
    return cid


# ---- FETCH ------------------------------------------------------------------

def fetch(c):
    """Download PDFs.

    Strategy:
    - aig/ URLs: live site is reachable → try live first; fallback to Wayback.
    - uploads/ URLs: live site is reachable → try live first.
    """
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url FROM dgacbo_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    failed = 0

    for row in rows:
        cid = row["case_id"]
        src_url = row["source_url"]
        safe_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[dgacbo fetch] {cid} ...", flush=True)

        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            c.execute(
                "UPDATE dgacbo_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
            continue

        # Try live first (both patterns are live on dgac.gob.bo)
        ok = curl_download(src_url, dest, max_time=30)
        if ok:
            sz = os.path.getsize(dest)
            c.execute(
                "UPDATE dgacbo_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved (live) {sz//1024}KB", flush=True)
            time.sleep(DELAY)
            continue

        # Fallback: Wayback CDX snapshot
        print(f"  live failed → trying Wayback...", flush=True)
        ts = cdx_best_snapshot(src_url)
        if ts:
            archive_url = f"{WAYBACK_BASE}/{ts}id_/{src_url}"
            content, status = http_get(archive_url)
            if content and status == 200 and content[:4] == b"%PDF":
                with open(dest, "wb") as fh:
                    fh.write(content)
                c.execute(
                    "UPDATE dgacbo_reports SET pdf_path=?, archive_url=?, archive_ts=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, archive_url, ts, now(), cid),
                )
                c.commit()
                downloaded += 1
                print(f"  saved (wayback ts={ts}) {len(content)//1024}KB", flush=True)
                time.sleep(DELAY)
                continue

        # Both failed
        print(f"  FAILED: {cid}", file=sys.stderr)
        c.execute(
            "UPDATE dgacbo_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
            (now(), cid),
        )
        c.commit()
        failed += 1
        time.sleep(DELAY)

    print(f"[dgacbo fetch] downloaded={downloaded} failed={failed}", flush=True)
    return downloaded


# ---- PARSE ------------------------------------------------------------------

def parse(c):
    """Extract metadata from fetched PDFs using pdftotext."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url FROM dgacbo_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    no_text = 0
    needs_ocr = 0

    for row in rows:
        old_cid = row["case_id"]
        pdf_path = row["pdf_path"]
        source_url = row["source_url"]
        print(f"[dgacbo parse] {old_cid}", flush=True)

        txt = extract_text(pdf_path)
        if len(txt) < FLOOR:
            print(f"  only {len(txt)} chars → needs OCR", flush=True)
            c.execute(
                "UPDATE dgacbo_reports SET narrative_text=?, status='skipped', skip_reason='needs-ocr', updated_at=? WHERE case_id=?",
                (txt or "", now(), old_cid),
            )
            c.commit()
            no_text += 1
            needs_ocr += 1
            continue

        fn = source_url.split('/')[-1]
        event_date = parse_date(txt, filename=fn)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)
        accid_code = parse_accid_code(txt, filename=fn)
        report_type = parse_report_type(txt, filename=fn)

        new_cid = build_case_id(accid_code, registration, event_date, source_url)

        if new_cid != old_cid:
            # Check collision
            existing = c.execute(
                "SELECT case_id FROM dgacbo_reports WHERE case_id=? AND case_id!=?",
                (new_cid, old_cid)
            ).fetchone()
            if existing:
                # Supersession: same ACCID code (old already exists)
                # Keep Final > Preliminary; if both are final keep newer
                print(f"  [parse] collision with {new_cid} (supersession candidate)", flush=True)
                old_type = c.execute(
                    "SELECT report_type FROM dgacbo_reports WHERE case_id=?", (new_cid,)
                ).fetchone()
                if old_type and old_type["report_type"] == "Final report" and report_type == "Preliminary report":
                    print(f"  [parse] keeping existing Final, marking this Preliminary as superseded", flush=True)
                    c.execute(
                        "UPDATE dgacbo_reports SET status='skipped', skip_reason='superseded', updated_at=? WHERE case_id=?",
                        (now(), old_cid),
                    )
                    c.commit()
                    continue
                elif old_type and old_type["report_type"] == "Preliminary report" and report_type == "Final report":
                    print(f"  [parse] upgrading {new_cid} to Final (this report)", flush=True)
                    c.execute(
                        "UPDATE dgacbo_reports SET status='skipped', skip_reason='superseded-by-final', updated_at=? WHERE case_id=?",
                        (new_cid, ),
                    )
                    c.commit()
                    # Current row gets the final case_id
                else:
                    # Both same type: keep existing, skip current
                    print(f"  [parse] collision, keeping existing {new_cid}", flush=True)
                    c.execute(
                        "UPDATE dgacbo_reports SET status='skipped', skip_reason='dup-cid', updated_at=? WHERE case_id=?",
                        (now(), old_cid),
                    )
                    c.commit()
                    continue

            print(f"  rename {old_cid} → {new_cid}", flush=True)
            c.execute(
                """UPDATE dgacbo_reports SET
                     case_id=?, narrative_text=?, probable_cause=?, event_date=?,
                     registration=?, aircraft=?, location=?, operator=?,
                     report_type=?, status='parsed', skip_reason=NULL, updated_at=?
                   WHERE case_id=?""",
                (new_cid, txt, probable_cause, event_date, registration,
                 aircraft, location, operator, report_type, now(), old_cid),
            )
        else:
            c.execute(
                """UPDATE dgacbo_reports SET
                     narrative_text=?, probable_cause=?, event_date=?,
                     registration=?, aircraft=?, location=?, operator=?,
                     report_type=?, status='parsed', skip_reason=NULL, updated_at=?
                   WHERE case_id=?""",
                (txt, probable_cause, event_date, registration, aircraft,
                 location, operator, report_type, now(), old_cid),
            )
        c.commit()
        parsed += 1
        print(f"  date={event_date} reg={registration} acft={aircraft!r} len={len(txt)}", flush=True)

    print(f"[dgacbo parse] parsed={parsed} no_text={no_text} needs_ocr={needs_ocr}", flush=True)
    return parsed, needs_ocr


# ---- PARSE-SKIPPED (OCR) ----------------------------------------------------

def parse_skipped(c):
    """Re-parse OCR-needed rows using OCR_REMOTE."""
    host = os.environ.get("OCR_REMOTE")
    if not host:
        print("[dgacbo parse-skipped] OCR_REMOTE not set, skipping", flush=True)
        return 0, 0

    rows = c.execute(
        "SELECT case_id, pdf_path, source_url FROM dgacbo_reports "
        "WHERE status='skipped' AND skip_reason='needs-ocr' AND pdf_path IS NOT NULL"
    ).fetchall()
    print(f"[dgacbo parse-skipped] {len(rows)} rows to OCR", flush=True)
    ocr_ok = 0
    still_blank = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        source_url = row["source_url"]
        print(f"  OCR: {cid} ...", flush=True)

        txt = ocr_extract(pdf_path)
        if len(txt) < FLOOR:
            print(f"    still only {len(txt)} chars", flush=True)
            still_blank += 1
            continue

        fn = source_url.split('/')[-1]
        event_date = parse_date(txt, filename=fn)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)
        accid_code = parse_accid_code(txt, filename=fn)
        report_type = parse_report_type(txt, filename=fn)
        new_cid = build_case_id(accid_code, registration, event_date, source_url)

        if new_cid != cid:
            existing = c.execute(
                "SELECT 1 FROM dgacbo_reports WHERE case_id=? AND case_id!=?",
                (new_cid, cid)
            ).fetchone()
            if not existing:
                c.execute(
                    """UPDATE dgacbo_reports SET
                         case_id=?, narrative_text=?, probable_cause=?, event_date=?,
                         registration=?, aircraft=?, location=?, operator=?,
                         report_type=?, status='parsed', skip_reason=NULL, updated_at=?
                       WHERE case_id=?""",
                    (new_cid, txt, probable_cause, event_date, registration,
                     aircraft, location, operator, report_type, now(), cid),
                )
                c.commit()
                ocr_ok += 1
                print(f"    OCR ok, renamed to {new_cid}, date={event_date}", flush=True)
                continue
            else:
                # supersession collision after OCR
                c.execute(
                    "UPDATE dgacbo_reports SET status='skipped', skip_reason='superseded', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

        c.execute(
            """UPDATE dgacbo_reports SET
                 narrative_text=?, probable_cause=?, event_date=?,
                 registration=?, aircraft=?, location=?, operator=?,
                 report_type=?, status='parsed', skip_reason=NULL, updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft,
             location, operator, report_type, now(), cid),
        )
        c.commit()
        ocr_ok += 1
        print(f"    OCR ok, date={event_date} reg={registration}", flush=True)

    print(f"[dgacbo parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank


# ---- BUILD ------------------------------------------------------------------

def build(c):
    """Write dgacbo_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, operator, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM dgacbo_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE dgacbo_reports SET status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        cid = r["case_id"]
        slug = cid.lower()

        c.execute(
            """INSERT OR REPLACE INTO dgacbo_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'BO', ?, ?, ?, ?, ?, 'es', ?)""",
            (
                cid,
                r["event_date"],
                r["aircraft"],
                r["registration"],
                r["operator"],
                r["location"],
                narr,
                r["probable_cause"],
                r["source_url"],
                r["report_type"] or "Final report",
                slug,
                now(),
            ),
        )
        c.execute(
            "UPDATE dgacbo_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), cid),
        )
        c.commit()
        built += 1

    print(f"[dgacbo build] built={built}", flush=True)
    return built


# ---- RECHECK ----------------------------------------------------------------

def recheck(c):
    """Re-query CDX for not-archived/failed PDFs; reset to 'new' if now available."""
    rows = c.execute(
        "SELECT case_id, source_url FROM dgacbo_reports "
        "WHERE status='skipped' AND skip_reason IN ('fetch-failed', 'not-archived')"
    ).fetchall()
    print(f"[dgacbo recheck] checking {len(rows)} failed URLs", flush=True)
    reset = 0
    for row in rows:
        ts = cdx_best_snapshot(row["source_url"])
        if ts:
            print(f"  [recheck] {row['case_id']} now in Wayback ts={ts}", flush=True)
            c.execute(
                "UPDATE dgacbo_reports SET status='new', skip_reason=NULL, updated_at=? WHERE case_id=?",
                (now(), row["case_id"]),
            )
            c.commit()
            reset += 1
    print(f"[dgacbo recheck] reset={reset}", flush=True)
    return reset


# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- status counts ---")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM dgacbo_reports GROUP BY status, skip_reason ORDER BY n DESC"):
        print(f"  {row['status']:10s}  {(row['skip_reason'] or ''):30s}  {row['n']}")
    cnt = c.execute("SELECT COUNT(*) FROM dgacbo_accidents").fetchone()[0]
    print(f"\n--- dgacbo_accidents: {cnt} rows ---")
    if cnt:
        row = c.execute(
            "SELECT SUM(event_date IS NULL) null_dates, "
            "SUM(event_date IS NOT NULL) dated, "
            "MIN(LENGTH(narrative_text)) min_len, MAX(LENGTH(narrative_text)) max_len "
            "FROM dgacbo_accidents"
        ).fetchone()
        print(f"  event_date: dated={row['dated']} NULL={row['null_dates']}")
        print(f"  narr_len:   min={row['min_len']} max={row['max_len']}")
        # Per-year distribution
        print("\n  per-year distribution:")
        for yr_row in c.execute(
            "SELECT substr(event_date,1,4) yr, count(*) n "
            "FROM dgacbo_accidents GROUP BY yr ORDER BY yr"
        ):
            print(f"    {yr_row['yr'] or 'NULL'}: {yr_row['n']}")
        # Sample rows
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, aircraft, LENGTH(narrative_text) len "
            "FROM dgacbo_accidents ORDER BY event_date LIMIT 15"
        ):
            print(
                f"    {r['case_id'][:40]:40s}  reg={r['registration'] or 'NULL':10s}"
                f"  date={r['event_date'] or 'NULL'}  acft={str(r['aircraft'] or 'NULL')[:20]:20s}"
                f"  len={r['len']}"
            )
    # Dups check
    dups = c.execute(
        "SELECT case_id, count(*) n FROM dgacbo_accidents GROUP BY case_id HAVING n > 1"
    ).fetchall()
    if dups:
        print(f"\n  ⚠️ DUPLICATE case_ids: {len(dups)}")
        for d in dups:
            print(f"    {d['case_id']}: {d['n']}")
    else:
        print("\n  dups=0 ✓")
    # needs-ocr count
    ocr_needed = c.execute(
        "SELECT count(*) FROM dgacbo_reports WHERE skip_reason='needs-ocr'"
    ).fetchone()[0]
    if ocr_needed:
        print(f"\n  ⚠️ needs-ocr: {ocr_needed} (run parse-skipped with OCR_REMOTE set)")


# ---- MAIN -------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "all"):
        discover(c)

    if mode in ("fetch", "all"):
        fetch(c)

    if mode in ("parse", "all"):
        parse(c)

    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}", flush=True)
        if ok:
            build(c)

    if mode in ("build", "all"):
        build(c)

    if mode == "recheck":
        reset = recheck(c)
        if reset:
            print("Run fetch → parse → build to ingest them.")

    if mode == "stats":
        pass  # just print_stats below

    print_stats(c)


if __name__ == "__main__":
    main()
