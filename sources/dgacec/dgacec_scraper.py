#!/usr/bin/env python3
"""DGACEC (Ecuador — Dirección General de Aviación Civil) aviation-accident ingest.

Source: aviacioncivil.gob.ec official investigation reports — recovered ENTIRELY
from the Wayback Machine because live aviacioncivil.gob.ec no longer serves these
PDFs (site restructured ~2020, original wp-content/uploads/downloads paths gone).
DO NOT probe aviacioncivil.gob.ec directly.

PDF URL patterns found via CDX:
  Most common:
    /wp-content/uploads/downloads/2015/08/<DD-DE-MONTH-DEL-YYYY-AIRCRAFT>.pdf
      e.g. 01-DE-SEPTIEMBRE-DEL-2010-CESSNA-A-188-B.pdf
           26-DE-NOVIEMBRE-DEL-2011-BELL-212.pdf
    /wp-content/uploads/downloads/2019/06/<aircraft_type-reg>.pdf
      e.g. bell_212.pdf, cessna_R-172-G.pdf, helio_COURIER-H-700.pdf
  Older (2018):
    /wp-content/uploads/downloads/2018/03/INFORME-FINAL-HC-COX-TAME-WEB.pdf

Enumeration strategy: CDX per-year queries with pagination (page=N) to beat the
5000-row cap. Years 2013–2019 per spec. Known rate-limit: 2s base + backoff on
429/503.  2013 and 2016 CDX queries frequently time out — handled with retry.

Filter: include only accident/incident investigation reports; exclude regulatory
docs (RDAC, resolucion, extracto, licencias, HR, financial).  Ambiguous files
go on skip_list with skip_reason='ambiguous'.

case_id: 'dgacec-' + intrinsic id from PDF text (AC-NN-YYYY, IN-NN-YYYY) when
present; else reg+date slug; else date+aircraft slug.  HC- registrations kept
verbatim (foreign registered aircraft operating in Ecuador, not a typo).

event_date: extracted from Spanish prose in PDF text (NOT from URL path which
is the upload year).  Patterns: '20 JULIO 2000', 'el 05 de febrero del 2002',
ISO dates in header, DD-DE-MONTH-DEL-YYYY in filename as last resort.

OCR: if >15% of fetched PDFs have pdftotext < FLOOR chars, activate OCR via
OCR_REMOTE=<ocr-host> (hetzner), lang 'spa'.  Run as separate
parse-skipped stage after normal parse completes.

Stages: discover | fetch | parse | parse-skipped | build | recheck | all
  recheck: re-query CDX for not-archived URLs.

Politeness: 2s base delay, exponential backoff on 429/503.
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile, uuid, urllib.request

DGACEC_BASE = "https://www.aviacioncivil.gob.ec"
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 2.0       # base inter-request delay (archive.org politeness)
FLOOR = 80        # minimum chars to consider text usable
HOME = os.path.expanduser("~/dgacec-ingest")
DB = os.path.join(HOME, "dgacec.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# CDX years to enumerate (upload years for accident reports)
CDX_YEARS = list(range(2013, 2020))

# OCR language for tesseract (Spanish)
OCR_LANG = "spa"

# ---- SCHEMA ------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS dgacec_reports (
  case_id       TEXT PRIMARY KEY,
  source_url    TEXT,        -- canonical aviacioncivil.gob.ec URL
  archive_url   TEXT,        -- Wayback snapshot URL used for download
  archive_ts    TEXT,        -- CDX timestamp of selected snapshot
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
CREATE TABLE IF NOT EXISTS dgacec_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'EC',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'es',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_dgacec_status ON dgacec_reports(status);
"""

# ---- FILTER: which filenames are investigation reports ----------------------

# Positive indicators: filename or path clearly identifies an accident report
_REPORT_INCLUDE = [
    # date-prefixed filenames: 01-DE-SEPTIEMBRE-DEL-2010-CESSNA-A-188-B.pdf
    re.compile(r'^\d{2}-DE-[A-Z]', re.I),
    # aircraft-type only slugs: bell_212.pdf, cessna_R-172-G.pdf
    re.compile(r'^(bell|cessna|piper|grumman|helio|ayres|trush|embraer|beech|mooney|air.tractor|pezetel|ultraligero|air.creation|pa-\d|avia|pzt)[_\s\-]', re.I),
    # INFORME-FINAL-HC-COX-TAME-WEB.pdf — named investigation report
    re.compile(r'informe.final.+(?:HC-|TAME|aerolin|aerogal|tame|LAN|lan.)', re.I),
    # Numbered with 00_, 01_, 02_ prefixed aviation investigation pattern
    re.compile(r'^\d{2}_avio', re.I),
    # AC- or IN- expediente in filename
    re.compile(r'(?:^|[_\-])(?:AC|IN)-\d{2}', re.I),
    # HC-XXX registration prefixed: HC-COX type
    re.compile(r'(?:^|[_\-])HC-[A-Z]{2,}', re.I),
]

# Negative indicators: clearly NOT investigation reports
_REPORT_EXCLUDE = [
    re.compile(r'(?i)RDAC'),           # regulatory documents
    re.compile(r'(?i)resoluci'),       # resolutions
    re.compile(r'(?i)extracto'),       # airline operations extracts
    re.compile(r'(?i)acuerdo'),        # administrative agreements
    re.compile(r'(?i)circular'),       # circulars
    re.compile(r'(?i)convocatoria'),   # job vacancy calls
    re.compile(r'(?i)concurso'),       # merit competitions
    re.compile(r'(?i)rendicion'),      # financial rendition reports
    re.compile(r'(?i)presupuest'),     # budget
    re.compile(r'(?i)prospecto'),      # course prospectus
    re.compile(r'(?i)equipo.de.trabajo'),  # work teams
    re.compile(r'(?i)convenio'),       # international conventions
    re.compile(r'(?i)instructivo|instruccion'),  # instruction docs
    re.compile(r'(?i)formulario'),     # forms
    re.compile(r'(?i)reglamento'),     # regulations
    re.compile(r'(?i)decreto'),        # decrees
    re.compile(r'(?i)manual'),         # manuals
    re.compile(r'(?i)ley[_\-]|ley\.'),  # laws
    re.compile(r'(?i)codigo[_\-]'),    # codes
    re.compile(r'(?i)hoja.de.vida'),   # CVs
    re.compile(r'(?i)-F-\d{3}'),       # pilot licence files (NAME-F-123)
    re.compile(r'(?i)CUR-\d+'),        # licence numbers
    re.compile(r'(?i)literal[_\-]'),   # LOTAIP transparency literals
    re.compile(r'(?i)LOTAIP'),
    re.compile(r'(?i)distributivo'),   # HR distribution
    re.compile(r'(?i)contrato.colectivo'),  # collective contracts
    re.compile(r'(?i)estatuto'),
    re.compile(r'(?i)planif|plan.de|poa_|pei-|pai-'),  # planning
    re.compile(r'(?i)reporte.de.gastos|reporte_gpr'),  # financial reports
    re.compile(r'(?i)informe.narrativo|informe.final.rendicion|informe.anual'),
    re.compile(r'(?i)informe.final.planta|informe.final.region'),
    re.compile(r'(?i)aerogal.nacional|aerolane|aeromexico|internaciona'),  # stats
    re.compile(r'(?i)resumen.y.comparativo'),
    re.compile(r'(?i)matriz.comprom|matriz.seguim'),  # audit tracking
    re.compile(r'(?i)destinatarios.recursos'),  # public beneficiary lists
    re.compile(r'(?i)cuadro.detalle'),
    re.compile(r'(?i)aeropuerto.ciudad'),  # airport profile docs
    re.compile(r'(?i)ETAC|COTAC|ISTAC|COTAC'),  # school names
    re.compile(r'(?i)^Aeropuerto'),
    re.compile(r'(?i)Constitucion.de.la.Republica'),
    re.compile(r'(?i)SA-CR-\d+'),      # security certification procedures
    re.compile(r'(?i)GSA-CR-\d+'),
    re.compile(r'(?i)R-MDT-'),         # labor ministry decisions
    re.compile(r'(?i)DGAC-YA-\d|DGAC-SX'),  # administrative DGAC resolutions
    re.compile(r'(?i)^decision.CAN'),
    re.compile(r'(?i)solicitud|preaplicacion'),
    re.compile(r'(?i)viaticos'),       # travel expense reports
    re.compile(r'(?i)nombramiento|designacion'),
    re.compile(r'(?i)7FFAA.REGLAMENTO'),
]


def is_investigation_report(url):
    """Return (include: bool, reason: str) for a URL."""
    decoded = urllib.parse.unquote(url)
    fn = decoded.split('/')[-1]

    # Check exclusions first (faster reject)
    for pat in _REPORT_EXCLUDE:
        if pat.search(fn):
            return False, f"excluded:{pat.pattern[:40]}"

    # Check inclusions
    for pat in _REPORT_INCLUDE:
        if pat.search(fn):
            return True, "included"

    # Ambiguous — default skip
    return False, "ambiguous"


# ---- OCR helpers (from aaid-ingest/aaid_ingest/pdf.py) ----------------------

def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote host. Returns '' on any failure."""
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
    """OCR a scanned PDF; returns text or '' on failure.

    Prefers OCR_REMOTE env (<ocr-host>) to run on hetzner,
    keeping heavy OCR off the loaded mini-PC.
    """
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

# "20 JULIO 2000", "05 de febrero del 2002", "el 20 de julio de 2000"
_ES_DATE_PAT = re.compile(
    r'\b(\d{1,2})\s+(?:de\s+)?(' + _ES_MONTH_ALT + r')(?:\s+de(?:l)?)?[\s,]+(\d{4})\b',
    re.IGNORECASE,
)
# "JULIO 20 DE 2000" or "JULIO 20, 2000"  (month first, rare)
_ES_DATE_MF = re.compile(
    r'\b(' + _ES_MONTH_ALT + r')\s+(\d{1,2})[,\s]+(?:de\s+)?(\d{4})\b',
    re.IGNORECASE,
)
# ISO / numeric: YYYY-MM-DD, DD/MM/YYYY, DD.MM.YYYY
_ISO_DATE = re.compile(
    r'\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b'
    r'|'
    r'\b([0-3]?\d)[.\-/]([01]?\d)[.\-/]((?:19|20)\d{2})\b'
)
# Filename pattern: 01-DE-SEPTIEMBRE-DEL-2010-CESSNA (last resort)
_FN_DATE_PAT = re.compile(
    r'^(\d{1,2})-DE-(' + _ES_MONTH_ALT + r')(?:-DEL?)?-(\d{4})-',
    re.IGNORECASE,
)


def _es_month(s):
    s = s.lower()
    for k in sorted(ES_MONTHS, key=len, reverse=True):
        if s.startswith(k):
            return ES_MONTHS[k]
    return None


def parse_date(txt, filename=None):
    """Parse event date from Spanish PDF text. Returns ISO YYYY-MM-DD or None.

    Priority:
    1. Spanish prose in first 3000 chars: '20 de julio de 2000', '05 FEBRERO 2002'
    2. ISO / numeric date in header
    3. Filename date as last resort (only when txt is blank)

    ⚠️ NEVER use the URL upload-year as the event date.
    """
    if txt:
        header = txt[:3000]
        # 1. Spanish day-month-year
        m = _ES_DATE_PAT.search(header)
        if m:
            d, month_str, y = int(m.group(1)), m.group(2), int(m.group(3))
            mo = _es_month(month_str)
            if mo and 1 <= d <= 31 and 1990 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        # 1b. month-first Spanish
        m = _ES_DATE_MF.search(header)
        if m:
            month_str, d, y = m.group(1), int(m.group(2)), int(m.group(3))
            mo = _es_month(month_str)
            if mo and 1 <= d <= 31 and 1990 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        # 2. ISO date
        m = _ISO_DATE.search(header)
        if m:
            if m.group(1):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
            if 1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"

    # 3. Filename date (last resort — only reasonable when PDF text is blank)
    if filename:
        fn = urllib.parse.unquote(filename).split('/')[-1]
        m = _FN_DATE_PAT.match(fn)
        if m:
            d, month_str, y = int(m.group(1)), m.group(2), int(m.group(3))
            mo = _es_month(month_str)
            if mo and 1 <= d <= 31 and 1990 <= y <= 2030:
                return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


# ---- METADATA EXTRACTION ----------------------------------------------------

# Ecuador registrations: HC-XXX (3-letter suffix for civil); military FAE-XXX
# NOTE: exclude AC-\d+ and IN-\d+ which are expediente codes, not registrations
_REG_RE = re.compile(r'\b(HC-[A-Z0-9]{2,5}|FAE-?[A-Z0-9]{2,5})\b')
# General ICAO-style reg fallback (non-HC): only if not an AC/IN expediente prefix
_REG_ICAO = re.compile(r'\b(?!AC-\d|IN-\d)([A-Z]{1,2}-[A-Z]{3,5})\b')

# Expediente patterns: AC-05-2000, IN-12-2001, AC-005-2000
# Also match expediente in filename: 00_avioDECO-PIJAO_AC-05 → AC-05 + event_year from text
_EXPEDIENTE_RE = re.compile(
    r'\b((?:AC|IN)-\s*\d{1,4}[-/]\s*(?:19|20)\d{2})\b',
    re.IGNORECASE,
)
# Standalone expediente (no year yet) — AC-05, IN-12 in filename context
_EXPEDIENTE_BARE = re.compile(r'(?:^|[_\-\s])((?:AC|IN)-\d{1,4})(?:[_\-\s.]|$)', re.IGNORECASE)


def parse_registration(txt):
    """Extract aircraft registration (prefer HC- prefix for Ecuador).

    Excludes AC-NN / IN-NN expediente codes from registration field.
    """
    if not txt:
        return None
    header = txt[:4000]
    # 1. HC- registration (most common Ecuador civil)
    m = re.search(r'\bHC-[A-Z0-9]{2,5}\b', header)
    if m:
        return m.group(0)
    # 2. FAE- military
    m = _REG_RE.search(header)
    if m:
        return m.group(1)
    # 3. Generic ICAO-style (e.g. foreign registered: N-XXXX, CP-XXX) —
    #    only if clearly labelled near 'matrícula', 'registro' or 'N°'
    m = re.search(r'(?:matr[íi]cula|registro|N[°º\.])\s*:?\s*([A-Z]{1,2}-[A-Z0-9]{2,5})\b', header, re.I)
    if m:
        cand = m.group(1)
        # Don't return if it's an expediente code
        if not re.match(r'^(?:AC|IN)-\d', cand, re.I):
            return cand
    return None


def parse_aircraft(txt):
    """Extract aircraft type from Spanish PDF text."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        # "Marca y Modelo de la aeronave:\n<value on next line>"
        # Must capture only a value line, not another field label
        r'(?:Marca y Modelo de la aeronave|Modelo de aeronave)[:\s]*\n([A-Za-z0-9][A-Za-z0-9\s\-/\.]{2,50})',
        # Inline field: "Tipo de aeronave: Cessna 172"
        r'(?:Tipo de aeronave)[:\s]+([A-Za-z0-9][A-Za-z0-9\s\-/\.]{2,50})',
        # Aircraft type in text body: "La aeronave Cessna A188-B cumplía..."
        r'La aeronave\s+([A-Za-z][A-Za-z0-9\s\-/\.]{2,40})\s+cum',
        # Inline: "aeronave Cessna A188-B"
        r'\baeronave\s+([A-Za-z][A-Za-z0-9\s\-/\.]{2,40})[,\.\n]',
        # Known aircraft make names with optional model suffixes
        r'\b((?:Cessna|Bell|Piper|Boeing|Airbus|Embraer|Grumman|Helio|Beech|Trush|Ayres|Pezetel|Air\s*Tractor|Ultraligero|Quicksilver|Air\s*Creation|Aerocomander|Aerocommander|DHC-|ATR-|Dornier|Fokker|Let\s+L-|Pilatus|Diamond)[\w\s\-/\.]{1,40})',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            # Reject generic section labels, stop-word phrases, and sentence fragments
            if 3 < len(v) < 60 and not re.match(
                r'^(de\s|la\s|el\s|en\s|que\s|un\s|una\s|los\s|las\s|y\s+el\s|tipo\s+de\s+oper|operaci|impacto|momento)',
                v, re.I
            ) and not re.search(r'\b(impacto|provoc|sufri|golpe|fractura|cuando|durante)\b', v, re.I):
                return v
    return None


def parse_location(txt):
    """Extract accident location from Spanish PDF text."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r'(?:Lugar del accidente|Lugar del incidente|Lugar del siniestro|Lugar de ocurrencia|Lugar)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][\w\s\-,./]{3,80})',
        r'(?:Ubicación|Ubicacion|Localización|localizacion)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][\w\s\-,./]{3,80})',
        r'(?:cerca de|próximo a|aledaños a|en el sector de|en la parroquia|en el cantón)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ\w\s,]{3,60})',
        r'(?:Provincia de|Cantón de|Parroquia de)\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ\w\s]{2,40})',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            if 3 < len(v) < 100:
                return v
    return None


def parse_operator(txt):
    """Extract operator from Spanish PDF text."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r'(?:Explotador|Operador|Propietario)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][\w\s\-,./]{3,80})',
        r'(?:Empresa|Compañía)[:\s]+([A-Za-zÁÉÍÓÚáéíóúÑñ][\w\s\-,./]{3,80})',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            if 2 < len(v) < 100:
                return v
    return None


def parse_probable_cause(txt):
    """Extract conclusions/probable cause from Spanish PDF text."""
    if not txt:
        return None
    for section_pat in [
        r'(?:CAUSAS PROBABLES?|CAUSA PROBABLE|CAUSAS?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:CONCLUSIONES?|CONCLUSIÓN)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:DETERMINACIÓN DE CAUSAS?|DETERMINACION DE CAUSAS?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:Causas probables?|Causa probable|Causas?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:Conclusiones?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
        r'(?:FACTORES CONTRIBUYENTES?|Factores contribuyentes?)[:\s]*\n(.*?)(?=\n[A-ZÁÉÍÓÚÑ]{3}|\Z)',
    ]:
        m = re.search(section_pat, txt, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1).strip()
            section = re.split(r'\n(?:[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{5,}|[0-9]+\.)\s*\n', section)[0]
            section = section[:2500].strip()
            if len(section) > 50:
                return section
    return None


def parse_expediente(txt, filename=None):
    """Extract DGAC expediente number: AC-05-2000, IN-12-2001 etc.

    If full AC-05-2000 pattern not found in text, tries to compose from a bare
    AC-05 / IN-12 found in the filename + event_year from parsed date.
    """
    if not txt:
        return None
    header = txt[:3000]
    m = _EXPEDIENTE_RE.search(header)
    if m:
        return re.sub(r'\s+', '', m.group(1).upper())
    # Try bare expediente in filename only — combine with event year from text
    if filename:
        fn = urllib.parse.unquote(filename).split('/')[-1]
        m2 = _EXPEDIENTE_BARE.search(fn)
        if m2:
            bare = m2.group(1).upper().replace(' ', '')
            # Try to get a year from text
            yr_m = re.search(r'\b((?:19|20)\d{2})\b', header)
            if yr_m:
                yr = int(yr_m.group(1))
                if 1990 <= yr <= 2025:
                    return f"{bare}-{yr}"
    return None


def parse_report_type(txt):
    """Classify as 'Final report' or 'Preliminary report' from text."""
    if not txt:
        return "Final report"
    header = txt[:2000].upper()
    if re.search(r'INFORME\s+PRELIMINAR|INFORME\s+PREVIO|PRELIMINARY', header):
        return "Preliminary report"
    return "Final report"


# ---- CASE-ID CONSTRUCTION ---------------------------------------------------

def build_case_id(expediente, registration, event_date, filename):
    """Build stable intrinsic case_id.

    Priority:
    1. Expediente from text: AC-05-2000 → 'dgacec-ac-05-2000'
    2. registration + event_date slug: 'dgacec-hc-cdg-2002-11-26'
    3. filename-derived slug (fallback)
    HC- registrations kept verbatim (foreign, not a typo).
    """
    if expediente:
        return "dgacec-" + re.sub(r'[^a-z0-9]+', '-', expediente.lower()).strip('-')

    if registration and event_date:
        reg_slug = re.sub(r'[^a-z0-9]+', '-', registration.lower()).strip('-')
        return "dgacec-" + reg_slug + "-" + event_date

    # Filename fallback
    fn = urllib.parse.unquote(filename).split('/')[-1]
    fn = re.sub(r'\.pdf$', '', fn, flags=re.I)
    slug = re.sub(r'[^a-z0-9]+', '-', fn.lower()).strip('-')[:80]
    return "dgacec-" + slug


# ---- CDX / HTTP HELPERS -----------------------------------------------------

def now():
    return int(time.time() * 1000)


def wayback_get_urllib(url, retries=4):
    """GET via urllib with exponential backoff on 429/503/504."""
    delay = DELAY
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                content = resp.read()
            time.sleep(delay)
            return content, 200
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 504):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] HTTP {e.code} → sleep {wait}s", flush=True)
                time.sleep(wait)
                delay = DELAY
                continue
            print(f"  [http] {e.code} for {url}", file=sys.stderr)
            return None, e.code
        except Exception as e:
            if attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"  [http error] {type(e).__name__}: {e} → sleep {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  [http fatal] {type(e).__name__}: {e}", file=sys.stderr)
    return None, 0


def cdx_enum_year(year, page=0):
    """Query CDX for one upload year+page. Returns list of [original, timestamp] rows."""
    url = (
        f"{CDX_BASE}?url=aviacioncivil.gob.ec/wp-content/uploads/downloads/{year}/*"
        f"&output=json&filter=mimetype:application/pdf&filter=statuscode:200"
        f"&fl=original,timestamp&collapse=urlkey&limit=5000&page={page}"
    )
    content, status = wayback_get_urllib(url)
    if not content or status != 200:
        return []
    try:
        data = json.loads(content)
        return data[1:] if data else []  # skip header row
    except Exception:
        return []


def cdx_best_snapshot(orig_url):
    """Query CDX for best (most recent 200) snapshot timestamp for a PDF URL."""
    url = (
        f"{CDX_BASE}?url={urllib.parse.quote(orig_url, safe=':/')}"
        f"&output=json&filter=statuscode:200&limit=5&fl=timestamp"
    )
    content, status = wayback_get_urllib(url)
    if not content or status != 200:
        return None
    try:
        data = json.loads(content)
        rows = data[1:] if data else []
        if not rows:
            return None
        return rows[-1][0]  # most recent timestamp
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


# ---- DISCOVER ----------------------------------------------------------------

def discover(c):
    """Enumerate Wayback CDX for all investigation report PDF URLs.

    Strategy: per-year CDX queries (2013–2019), paginated (page=0,1,…) to beat
    the 5000-row CDX cap.  Each URL is then filtered through is_investigation_report()
    before insertion into dgacec_reports.
    """
    inserted = 0
    skipped_regulatory = 0
    skipped_ambiguous = 0
    seen_urls = set()

    # Pre-load known URLs to avoid duplicates on rerun
    for row in c.execute("SELECT source_url FROM dgacec_reports"):
        seen_urls.add(row["source_url"])

    for year in CDX_YEARS:
        page = 0
        while True:
            print(f"[dgacec discover] year={year} page={page}...", flush=True)
            rows = cdx_enum_year(year, page)
            print(f"  → {len(rows)} CDX rows", flush=True)

            for r in rows:
                orig_url = r[0]
                # Normalise: strip port 80
                orig_url = re.sub(r'(https?://[^/]+):80/', r'\1/', orig_url)

                if orig_url in seen_urls:
                    continue
                seen_urls.add(orig_url)

                include, reason = is_investigation_report(orig_url)
                if not include:
                    if "ambiguous" in reason:
                        skipped_ambiguous += 1
                    else:
                        skipped_regulatory += 1
                    continue

                # Generate preliminary case_id from URL (will be refined in parse)
                fn = urllib.parse.unquote(orig_url).split('/')[-1]
                fn_slug = re.sub(r'[^a-z0-9]+', '-', re.sub(r'\.pdf$', '', fn.lower())).strip('-')[:80]
                prelim_id = "dgacec-" + fn_slug

                # Handle collision (two different URLs → same slug)
                base_id = prelim_id
                suffix = 0
                while c.execute("SELECT 1 FROM dgacec_reports WHERE case_id=?", (prelim_id,)).fetchone():
                    suffix += 1
                    prelim_id = base_id + f"-{suffix}"

                c.execute(
                    "INSERT OR IGNORE INTO dgacec_reports "
                    "(case_id, source_url, lang, status, discovered_at, updated_at) "
                    "VALUES (?, ?, 'es', 'new', ?, ?)",
                    (prelim_id, orig_url, now(), now()),
                )
                c.commit()
                inserted += 1

            # Pagination: stop when fewer than 5000 rows returned
            if len(rows) < 5000:
                break
            page += 1
            time.sleep(DELAY)

        time.sleep(DELAY)  # politeness between years

    print(
        f"[dgacec discover] inserted={inserted} "
        f"skipped_regulatory={skipped_regulatory} "
        f"skipped_ambiguous={skipped_ambiguous}",
        flush=True,
    )
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c):
    """Download PDFs via Wayback CDX → best snapshot URL.

    Large PDFs (>500KB) are fetched via curl for reliability.
    """
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url FROM dgacec_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    not_archived = []

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        # safe filename: use case_id slug
        safe_name = re.sub(r'[^A-Za-z0-9_.\-]', '_', cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[dgacec fetch] {cid} ...", flush=True)

        # Already downloaded?
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            ts_cached = c.execute(
                "SELECT archive_ts FROM dgacec_reports WHERE case_id=?", (cid,)
            ).fetchone()
            if ts_cached and ts_cached["archive_ts"]:
                c.execute(
                    "UPDATE dgacec_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
                downloaded += 1
                continue

        # CDX lookup for best snapshot
        ts = cdx_best_snapshot(orig_url)
        if not ts:
            print(f"  [dgacec fetch] {cid}: not archived", flush=True)
            not_archived.append(cid)
            c.execute(
                "UPDATE dgacec_reports SET status='skipped', skip_reason='not-archived', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
        print(f"  ts={ts}", flush=True)

        # Try urllib first; fall back to curl for large files
        content, status = wayback_get_urllib(archive_url)
        if not content or status != 200:
            # Fallback: curl
            print(f"  [fetch] urllib failed ({status}), trying curl...", flush=True)
            try:
                r = subprocess.run(
                    ["curl", "-sL", "--max-time", "120", "-A", UA,
                     "-o", dest, archive_url],
                    capture_output=True, timeout=150,
                )
                if r.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 500:
                    c.execute(
                        "UPDATE dgacec_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue
                # Verify PDF magic
                with open(dest, 'rb') as f:
                    magic = f.read(4)
                if magic != b'%PDF':
                    os.unlink(dest)
                    c.execute(
                        "UPDATE dgacec_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue
                c.execute(
                    "UPDATE dgacec_reports SET pdf_path=?, archive_url=?, archive_ts=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, archive_url, ts, now(), cid),
                )
                c.commit()
                downloaded += 1
                print(f"  saved (curl) {os.path.getsize(dest)//1024}KB", flush=True)
                continue
            except Exception as e:
                print(f"  [fetch curl] {cid}: {e}", file=sys.stderr)
                c.execute(
                    "UPDATE dgacec_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (str(e)[:120], now(), cid),
                )
                c.commit()
                continue

        # Verify PDF magic bytes
        if content[:4] != b"%PDF":
            print(f"  [dgacec fetch] {cid}: not a PDF (got {content[:20]!r})", file=sys.stderr)
            c.execute(
                "UPDATE dgacec_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        with open(dest, "wb") as fh:
            fh.write(content)
        c.execute(
            "UPDATE dgacec_reports SET pdf_path=?, archive_url=?, archive_ts=?, status='fetched', updated_at=? WHERE case_id=?",
            (dest, archive_url, ts, now(), cid),
        )
        c.commit()
        downloaded += 1
        print(f"  saved {len(content)//1024}KB", flush=True)

    print(f"[dgacec fetch] downloaded={downloaded} not_archived={len(not_archived)}", flush=True)
    return downloaded, not_archived


# ---- PARSE -------------------------------------------------------------------

def parse(c):
    """Extract text + metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url FROM dgacec_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    no_text = 0

    for row in rows:
        old_cid = row["case_id"]
        pdf_path = row["pdf_path"]
        source_url = row["source_url"]
        print(f"[dgacec parse] {old_cid}", flush=True)

        txt = extract_text(pdf_path)
        if len(txt) < FLOOR:
            print(f"  only {len(txt)} chars → no-text", flush=True)
            c.execute(
                "UPDATE dgacec_reports SET narrative_text=?, status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (txt or "", now(), old_cid),
            )
            c.commit()
            no_text += 1
            continue

        fn = source_url.split('/')[-1]
        event_date = parse_date(txt, filename=fn)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)
        expediente = parse_expediente(txt, filename=fn)  # pass filename for bare AC-XX extraction
        report_type = parse_report_type(txt)

        # Build final case_id from extracted data
        new_cid = build_case_id(expediente, registration, event_date, source_url)

        # Rename row if case_id changed (expediente/reg discovered in text)
        if new_cid != old_cid:
            # Check for collision
            existing = c.execute(
                "SELECT case_id FROM dgacec_reports WHERE case_id=? AND case_id!=?",
                (new_cid, old_cid)
            ).fetchone()
            if not existing:
                print(f"  [parse] rename {old_cid} -> {new_cid}", flush=True)
                # Insert new row with merged data, delete old
                c.execute(
                    """UPDATE dgacec_reports SET
                         case_id=?, narrative_text=?, probable_cause=?, event_date=?,
                         registration=?, aircraft=?, location=?, operator=?,
                         report_type=?, status='parsed', updated_at=?
                       WHERE case_id=?""",
                    (new_cid, txt, probable_cause, event_date, registration,
                     aircraft, location, operator, report_type, now(), old_cid),
                )
                c.commit()
                parsed += 1
                continue
            else:
                print(f"  [parse] collision on {new_cid}, keeping {old_cid}", flush=True)

        c.execute(
            """UPDATE dgacec_reports SET
                 narrative_text=?, probable_cause=?, event_date=?,
                 registration=?, aircraft=?, location=?, operator=?,
                 report_type=?, status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft,
             location, operator, report_type, now(), old_cid),
        )
        c.commit()
        parsed += 1

    print(f"[dgacec parse] parsed={parsed} no_text={no_text}", flush=True)

    # OCR threshold check: if >15% are no-text, print warning
    total = parsed + no_text
    if total > 0 and no_text / total > 0.15:
        print(
            f"[dgacec parse] ⚠️ WARN: {no_text}/{total} ({no_text*100//total}%) are no-text — "
            f"consider running parse-skipped with OCR_REMOTE=<ocr-host>",
            flush=True,
        )

    return parsed, no_text


# ---- PARSE-SKIPPED (OCR fallback) -------------------------------------------

def parse_skipped(c):
    """Re-parse no-text rows via OCR (spa tesseract on hetzner)."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url "
        "FROM dgacec_reports WHERE status='skipped' AND skip_reason='no-text'"
    ).fetchall()
    print(f"[dgacec parse-skipped] {len(rows)} no-text rows to OCR", flush=True)
    ocr_ok = 0
    still_blank = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        source_url = row["source_url"]
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  [parse-skipped] {cid}: PDF missing", file=sys.stderr)
            still_blank += 1
            continue
        print(f"  [parse-skipped] OCR {cid}...", flush=True)
        txt = ocr_extract(pdf_path, lang=OCR_LANG)
        if len(txt) < FLOOR:
            print(f"    still blank after OCR ({len(txt)} chars)", file=sys.stderr)
            still_blank += 1
            continue

        fn = source_url.split('/')[-1]
        event_date = parse_date(txt, filename=fn)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)
        expediente = parse_expediente(txt, filename=fn)
        report_type = parse_report_type(txt)
        new_cid = build_case_id(expediente, registration, event_date, source_url)

        if new_cid != cid:
            existing = c.execute(
                "SELECT 1 FROM dgacec_reports WHERE case_id=? AND case_id!=?",
                (new_cid, cid)
            ).fetchone()
            if not existing:
                c.execute(
                    """UPDATE dgacec_reports SET
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

        c.execute(
            """UPDATE dgacec_reports SET
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

    print(f"[dgacec parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank


# ---- BUILD -------------------------------------------------------------------

def build(c):
    """Write dgacec_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, operator, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM dgacec_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE dgacec_reports SET status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        cid = r["case_id"]  # already prefixed 'dgacec-'
        # site_slug: lowercase case_id, already URL-safe from build_case_id
        slug = cid.lower()

        c.execute(
            """INSERT OR REPLACE INTO dgacec_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'EC', ?, ?, ?, ?, ?, 'es', ?)""",
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
            "UPDATE dgacec_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), cid),
        )
        c.commit()
        built += 1

    print(f"[dgacec build] built={built}", flush=True)
    return built


# ---- RECHECK ----------------------------------------------------------------

def recheck(c):
    """Re-query CDX for not-archived PDFs; reset status='new' if now available."""
    rows = c.execute(
        "SELECT case_id, source_url FROM dgacec_reports "
        "WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    print(f"[dgacec recheck] checking {len(rows)} not-archived URLs", flush=True)
    newly_available = 0
    for row in rows:
        ts = cdx_best_snapshot(row["source_url"])
        if ts:
            print(f"  [recheck] {row['case_id']} now archived ts={ts}", flush=True)
            c.execute(
                "UPDATE dgacec_reports SET status='new', skip_reason=NULL, updated_at=? WHERE case_id=?",
                (now(), row["case_id"]),
            )
            c.commit()
            newly_available += 1
    print(f"[dgacec recheck] newly_available={newly_available}", flush=True)
    return newly_available


# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- status counts ---")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM dgacec_reports GROUP BY status, skip_reason"):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):25s} {row['n']}")
    cnt = c.execute("SELECT COUNT(*) FROM dgacec_accidents").fetchone()[0]
    print(f"\n--- dgacec_accidents: {cnt} rows ---")
    if cnt:
        row = c.execute(
            "SELECT SUM(event_date IS NULL), "
            "MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM dgacec_accidents"
        ).fetchone()
        print(f"  event_date NULL: {row[0]}  narr_len min={row[1]} max={row[2]}")
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, aircraft, LENGTH(narrative_text) len "
            "FROM dgacec_accidents ORDER BY event_date LIMIT 10"
        ):
            print(
                f"    {r['case_id'][:40]:40s}  reg={r['registration'] or 'NULL':12s}"
                f"  date={r['event_date'] or 'NULL'}  acft={r['aircraft'] or 'NULL'!r:25s}"
                f"  narr={r['len']}"
            )
    # Not-archived
    na = c.execute(
        "SELECT case_id FROM dgacec_reports WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    if na:
        print(f"\n  not-archived ({len(na)}): {[r['case_id'] for r in na[:10]]}")


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
        newly = recheck(c)
        print(f"recheck: {newly} newly available")
        if newly:
            print("Run 'fetch' then 'parse' then 'build' to ingest them.")

    print_stats(c)


if __name__ == "__main__":
    main()
