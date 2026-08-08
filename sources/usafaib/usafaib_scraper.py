#!/usr/bin/env python3
"""USAF Accident Investigation Board (AIB) reports ingest — httpx, no browser needed.

Source: AFJAG (Air Force Judge Advocate General) public AIB reports page:
  https://www.afjag.af.mil/AIB-Reports/
Single page with per-year zozo-tabs listing aircraft mishap AIB reports.
Each entry: "DD MON YY, COMMAND, AIRCRAFT, LOCATION, AIB REPORT"
Links are either direct /Portals/77/*.pdf or /LinkClick.aspx?fileticket= redirects
(both resolve to PDF when called with Firefox UA + Referer header).

VANTAGE: mini-PC works with Firefox UA; Chrome UA and mac/hetzner = 403.
Access-Denied for non-Firefox UA; gzip-encoded response requires Accept-Encoding.

Site year range: 2010, 2015-2019, 2020-2025 (tabs on main page).
Total ~96 AIB+GAIB report entries (some non-aircraft: GAIB ground/vehicle mishaps included).

Stages: discover (main page -> usafaib_reports) | fetch (download PDF) |
parse (pdftotext, OCR only if needed) | build (usafaib_accidents).
Resumable via status column.

Output columns: case_id, event_date, aircraft, registration, operator, location,
country, narrative_text, probable_cause, source_url, report_type, site_slug,
lang='en', built_at.
"""
import sys, os, re, time, sqlite3, shlex, subprocess, tempfile, uuid

BASE_URL  = "https://www.afjag.af.mil"
LISTING   = BASE_URL + "/AIB-Reports/"
DELAY     = 2.0          # seconds between HTTP requests (gentle)
MIN_NARRATIVE = 600      # preferred: full report text
FLOOR     = 80           # absolute build floor
MAX_PDF_BYTES = 60 * 1024 * 1024   # 60 MB hard skip
HOME      = os.path.expanduser("~/usafaib-ingest")
DB        = os.path.join(HOME, "usafaib.db")
PDFDIR    = os.path.join(HOME, "pdfs")
OCR_LANG  = "eng"

# Firefox UA is required — Chrome/default UA returns 403 from Akamai WAF
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usafaib_reports (
  case_id       TEXT PRIMARY KEY,
  year          INT,
  report_url    TEXT,   -- resolved direct PDF URL (after following LinkClick if needed)
  raw_href      TEXT,   -- original href from listing page
  pdf_path      TEXT,
  anchor_text   TEXT,   -- raw listing entry text e.g. "24 JAN 25, PACAF, F-35A, Eielson AFB, AIB REPORT"
  report_type   TEXT,   -- 'AIB report' or 'GAIB report'
  aircraft      TEXT,
  registration  TEXT,   -- tail number / serial e.g. 19-5535
  operator      TEXT,   -- command e.g. 'PACAF' / 'ACC' / 'U.S. Air Force'
  event_date    TEXT,   -- ISO YYYY-MM-DD
  location      TEXT,
  country       TEXT,   -- country of mishap
  narrative_text TEXT,
  probable_cause TEXT,
  source_tier   TEXT,   -- 'pdf', 'ocr', 'scanned', 'none', 'toolarge', 'fetchfail'
  lang          TEXT    DEFAULT 'en',
  status        TEXT    DEFAULT 'new',
  discovered_at INT,
  updated_at    INT
);
CREATE TABLE IF NOT EXISTS usafaib_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'en',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_usafaib_status ON usafaib_reports(status);
CREATE INDEX IF NOT EXISTS idx_usafaib_year   ON usafaib_reports(year);
"""

# ---- MONTH ABBREVIATION MAP ----
_MON = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,  'MAY': 5,  'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'SEPT': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    'JANUARY': 1, 'FEBRUARY': 2, 'MARCH': 3, 'APRIL': 4,  'JUNE': 6,
    'JULY': 7, 'AUGUST': 8, 'SEPTEMBER': 9, 'OCTOBER': 10, 'NOVEMBER': 11, 'DECEMBER': 12,
}

# Listing entry date formats: "24 JAN 25", "24 JAN 2025", "3 APR 2024"
# Also seen: "22 APR 2024" in anchor text
_ANCHOR_DATE_RE = re.compile(
    r'\b(\d{1,2})\s+(' + '|'.join(_MON.keys()) + r')\.?\s+(\d{2,4})\b',
    re.IGNORECASE,
)

# Report header date: "DATE OF ACCIDENT: 28 JANUARY 2025" or "28 JANUARY 2025"
_REPORT_DATE_RE = re.compile(
    r'(?:DATE\s+OF\s+(?:ACCIDENT|MISHAP|INCIDENT)\s*:?\s*)?'
    r'(\d{1,2})\s+(' + '|'.join(_MON.keys()) + r')\.?\s+(\d{4})\b',
    re.IGNORECASE,
)

# Tail number / serial: "T/N 19-5535" or "T/N 87-0024"
_TN_RE = re.compile(r'\bT/N\s+([A-Z0-9]{1,3}-?[0-9]{3,6})\b', re.IGNORECASE)

# Cause patterns in report text
_CAUSE_RE = re.compile(
    r'(?:cause\s+of\s+the\s+mishap\s+was|'
    r'AIB\s+president\s+found[^.]{0,80}cause[^.]{0,30}was|'
    r'cause\s+of\s+the\s+accident\s+was|'
    r'primary\s+cause\s+was)'
    r'\s*(.{80,600}?)(?:\.\s+(?:[A-Z]|\n)|$)',
    re.IGNORECASE | re.DOTALL,
)

# ---- helpers ----
def now():
    return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def http():
    import httpx
    return httpx.Client(
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        },
        timeout=90.0,
        follow_redirects=True,
    )

# ---- OCR helpers (copied from aaid-ingest / svk-ingest pattern) ----

def _ocr_remote(pdf_path, lang, host):
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

# ---- pdftotext ----

def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                              capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

# ---- slug / id helpers ----

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p]))
    return s.strip("-").lower()[:80] or None

def _parse_anchor_date(text):
    """Parse date from listing anchor text like '24 JAN 25, ...' -> 'YYYY-MM-DD'."""
    m = _ANCHOR_DATE_RE.search(text)
    if not m:
        return None, None, None
    d, mon_str, yr = int(m.group(1)), m.group(2).upper().rstrip('.'), m.group(3)
    mo = _MON.get(mon_str)
    if not mo:
        return None, None, None
    yr = int(yr)
    if yr < 100:
        yr += 2000  # 2-digit: 25 -> 2025
    return f"{yr:04d}-{mo:02d}-{d:02d}", yr, m

def parse_anchor_fields(anchor_text):
    """Extract event_date, year, command/operator, aircraft, location from anchor text.

    Anchor format examples:
      "24 JAN 25, PACAF, F-35A, Eielson AFB, AIB REPORT"
      "3 APR 2024, AETC, FORT NOVOSEL, TH-1H MISHAP ACCIDENT INVESTIGATION BOARD REPORT"
      "2 MAR 22, USAFE, F-16CG, AVIANO AB, ITALY AIB REPORT"
    """
    text = re.sub(r'\s+', ' ', anchor_text).strip()

    event_date, year, date_m = _parse_anchor_date(text)

    # After stripping the date prefix, split by comma
    # Remove date portion from beginning
    if date_m:
        remainder = text[date_m.end():].lstrip(', ').strip()
    else:
        remainder = text

    # Remove trailing "AIB REPORT" / "GAIB REPORT" / "ACCIDENT INVESTIGATION BOARD REPORT"
    remainder = re.sub(
        r',?\s*(?:AIRCRAFT\s+ACCIDENT\s+)?(?:MISHAP\s+)?'
        r'(?:ACCIDENT\s+INVESTIGATION\s+BOARD|AIB|GAIB)\s+(?:NARRATIVE\s+)?REPORT\s*$',
        '', remainder, flags=re.IGNORECASE).strip().rstrip(',').strip()

    parts = [p.strip() for p in remainder.split(',') if p.strip()]

    command = parts[0] if parts else None
    aircraft = parts[1] if len(parts) > 1 else None
    location_parts = parts[2:] if len(parts) > 2 else []
    location = ', '.join(location_parts) if location_parts else None

    # Detect ground/non-aircraft mishaps by aircraft field
    is_ground = False
    if aircraft:
        ground_kw = ['HUMMWV', 'HUMVEE', 'ATV', 'VEHICLE', 'MOTOR VEHICLE',
                     'SWIM', 'PARACHUTE', 'FIRE', 'CLIMBING', 'PMEL',
                     'FITNESS', 'POOL', 'GROUND', 'WATER VEHICLE']
        if any(kw in aircraft.upper() for kw in ground_kw):
            is_ground = True

    rtype = 'GAIB report' if is_ground else 'AIB report'

    return event_date, year, command, aircraft, location, rtype

def build_case_id(event_date, aircraft, location):
    """Build a stable, globally-unique case_id prefixed 'usafaib-'.

    Derived from date + aircraft-type-slug + location-slug so it is stable
    across re-runs (does not depend on listing order or URL).
    """
    # Normalize aircraft: drop trailing variants (F-16C/D -> f-16c, F-35A -> f-35a)
    acft_slug = re.sub(r'[^A-Za-z0-9]+', '-', (aircraft or 'unknown')).strip('-').lower()[:20]
    # Normalize location: first meaningful token
    loc_slug = re.sub(r'[^A-Za-z0-9]+', '-', (location or 'unknown')).strip('-').lower()[:20]
    date_part = (event_date or 'unknown-date').replace('/', '-')
    raw = f"usafaib-{date_part}-{acft_slug}-{loc_slug}"
    # Collapse repeating dashes
    return re.sub(r'-{2,}', '-', raw).rstrip('-')

def infer_country(location, anchor_text):
    """Infer mishap country from location/anchor text.

    Most mishaps are in the US; overseas bases and deployment areas are
    identifiable from common location markers.
    """
    combined = f"{location or ''} {anchor_text or ''}".upper()
    # Well-known overseas markers
    overseas = [
        ('JAPAN', 'JP'), ('KADENA', 'JP'), ('MISAWA', 'JP'), ('YOKOTA', 'JP'),
        ('GERMANY', 'DE'), ('AVIANO', 'IT'), ('ITALY', 'IT'),
        ('SOUTH KOREA', 'KR'), ('KOREA', 'KR'), ('KUNSAN', 'KR'), ('OSAN', 'KR'),
        ('AFGHANISTAN', 'AF'), ('BAGRAM', 'AF'),
        ('IRAQ', 'IQ'), ('ALI AL SALEM', 'KW'), ('KUWAIT', 'KW'),
        ('ENGLAND', 'GB'), ('LAKENHEATH', 'GB'), ('UK', 'GB'),
        ('SPANGDAHLEM', 'DE'), ('RAMSTEIN', 'DE'),
        ('TINIAN', 'MP'),  # Northern Mariana Islands (US territory, still distinct)
        ('ROKKASHO', 'JP'), ('YAKUSHIMA', 'JP'),
        ('ROK', 'KR'),
        ('AFRICOM', 'AF'),   # ambiguous; mark as 'Unknown' via final fallback
        ('EUCOM', 'DE'),
        ('CENTCOM', None),   # truly undisclosed
    ]
    for marker, cc in overseas:
        if marker in combined:
            return cc or 'Unknown'
    # US military bases: ANGB, AFB, NAS, etc. inside the US
    return 'US'

# ---- parse PDF text for metadata ----

_KNOWN_COMMANDS = frozenset([
    'ACC', 'AMC', 'AETC', 'AFGSC', 'AFMC', 'AFSC', 'AFSOC',
    'PACAF', 'USAFE', 'USAFE-AFAFRICA', 'AFSPC', 'AFRC', 'ANG',
])

# Known aircraft type prefixes for detecting when parts[0] is actually an aircraft
_ACFT_PREFIX_RE = re.compile(
    r'^(?:F-|A-|B-|C-|E-|T-|RQ-|MQ-|HH-|UH-|CH-|CV-|KC-|RC-|U-|P-|SR-|AC-|MC-|EC-)'
    r'|^(?:F22|F35|B2|B52|C130|C17|KC46)',
    re.IGNORECASE,
)

def extract_aircraft_from_header(txt):
    """Extract aircraft type from the PDF report header (first 800 chars).

    AIB reports open with lines like:
      UNITED STATES AIR FORCE
      AIRCRAFT ACCIDENT INVESTIGATION
      BOARD REPORT
      F-35A, T/N 19-5535
    or:
      F-22A, T/N 06-4109
      NELLIS AIR FORCE BASE, NEVADA
    Returns e.g. 'F-35A', 'MQ-9A', 'CV-22B', or None.
    """
    if not txt:
        return None
    header = txt[:800].upper()
    # Pattern: aircraft type on its own line, often followed by ", T/N" or ", TIN"
    m = re.search(
        r'^\s*((?:F|A|B|C|E|T|RQ|MQ|HH|UH|CH|CV|KC|RC|U|P|SR|AC|MC|EC)-'
        r'\d+[A-Z]?(?:[A-Z]|\+)?(?:/[A-Z])?),?\s*(?:T/?IN?\b|,)',
        header, re.MULTILINE,
    )
    if m:
        return m.group(1).strip().rstrip(',').title().replace(' ', '')
    # Also try pattern without T/N: line that is just an aircraft designation
    m2 = re.search(
        r'^\s*((?:F|A|B|C|E|T|RQ|MQ|HH|UH|CH|CV|KC|RC|U|P|SR|AC|MC|EC)-'
        r'\d+[A-Z]?(?:[A-Z]|\+)?)\s*$',
        header, re.MULTILINE,
    )
    if m2:
        return m2.group(1).strip()
    return None

def parse_report_text(txt):
    """Extract registration (T/N), date, probable cause, and aircraft from PDF text."""
    registration = None
    event_date = None
    probable_cause = None

    if not txt:
        return registration, event_date, probable_cause

    header = txt[:3000]  # most metadata is in first 3K chars

    # Tail number
    tn_m = _TN_RE.search(header)
    if tn_m:
        registration = tn_m.group(1).strip()

    # Event date from report header (more precise than anchor)
    d_m = _REPORT_DATE_RE.search(header)
    if d_m:
        d, mon_str, yr = int(d_m.group(1)), d_m.group(2).upper().rstrip('.'), int(d_m.group(3))
        mo = _MON.get(mon_str)
        if mo and 1 <= d <= 31 and 1900 < yr < 2100:
            event_date = f"{yr:04d}-{mo:02d}-{d:02d}"

    # Probable cause: search first 8K chars (executive summary area)
    cause_area = txt[:8000]
    cm = _CAUSE_RE.search(cause_area)
    if cm:
        pc = re.sub(r'\s+', ' ', cm.group(1)).strip()
        # Trim at paragraph boundary
        pc = re.split(r'\n\n|\r\n\r\n', pc)[0].strip()
        if len(pc) > 40:
            probable_cause = pc[:800]

    # Aircraft type from header
    aircraft_from_pdf = extract_aircraft_from_header(txt[:800])

    return registration, event_date, probable_cause, aircraft_from_pdf

def strip_boilerplate(txt):
    """Remove cover-page boilerplate and attachments TOC from narrative text.

    AIB reports structure: cover page -> executive summary -> summary of facts
    -> statement of opinion -> (long attachments index).
    We want: executive summary + statement of opinion (first ~10K chars is sufficient).
    """
    if not txt:
        return txt

    # Find start of substantive content
    # Look for EXECUTIVE SUMMARY or SUMMARY OF FACTS as start anchor
    for marker in ['EXECUTIVE SUMMARY', 'SUMMARY OF FACTS', 'ACCIDENT SUMMARY',
                   'SECTION I', 'PART I\n', 'I. ACCIDENT SUMMARY']:
        idx = txt.upper().find(marker)
        if idx >= 0 and idx < 5000:
            txt = txt[idx:]
            break

    # Trim attachments list (Tab A, Tab B, ... appears near end after page 10)
    # Look for patterns that indicate we're in the attachments index
    attachments_markers = [
        r'\nTab\s+[A-Z]\s*\n.*\nTab\s+[A-Z]',
        r'\nTAB\s+[A-Z]\b.*\nTAB\s+[A-Z]\b',
        r'\n[A-Z]{1,2}\s+Witness\s+Testimony',
        r'APPLICABLE REGULATIONS',
    ]
    for pat in attachments_markers:
        m = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
        if m and m.start() > 2000:
            txt = txt[:m.start()]
            break

    return txt.strip()

# ---- resolve LinkClick -> direct PDF URL ----

def resolve_href(cl, raw_href):
    """Resolve a /LinkClick.aspx?fileticket= URL to its final PDF URL.

    Returns (resolved_url, content_length_hint) or (raw_url, -1) on failure.
    HEAD request follows the redirect chain; the final URL is the direct PDF.
    """
    if '/LinkClick.aspx' not in raw_href and 'fileticket=' not in raw_href:
        # Already a direct URL
        url = raw_href if raw_href.startswith('http') else BASE_URL + raw_href
        return url, -1

    # Need the Referer to pass Akamai check
    url = raw_href if raw_href.startswith('http') else BASE_URL + raw_href

    # Some LinkClick variants appear as "Portals/77/LinkClick.aspx..." (missing leading /)
    if url.startswith('Portals/'):
        url = BASE_URL + '/' + url

    try:
        # HEAD with follow_redirects to get the final URL and content-length
        r = cl.head(url, headers={"Referer": LISTING})
        final_url = str(r.url)
        cl_val = r.headers.get('content-length', '-1')
        try:
            cl_int = int(cl_val)
        except ValueError:
            cl_int = -1
        return final_url, cl_int
    except Exception as e:
        print(f"[usafaib resolve] {url}: {e}", file=sys.stderr)
        return url, -1

# ---- stages ----

def discover(c, cl):
    """Fetch the main AIB-Reports page and parse all report entries into usafaib_reports."""
    print("[usafaib discover] fetching main listing...", flush=True)

    cl.headers["Referer"] = "https://www.afjag.af.mil/"
    try:
        r = cl.get(LISTING)
        time.sleep(DELAY)
    except Exception as e:
        print(f"[usafaib discover] fetch failed: {e}", file=sys.stderr)
        return 0

    if r.status_code != 200:
        print(f"[usafaib discover] HTTP {r.status_code}", file=sys.stderr)
        return 0

    html = r.text

    # Extract year tabs in order (used to infer missing year from anchor text)
    year_order = re.findall(r'<li><a>(20[12][0-9]|2010)</a></li>', html)
    print(f"[usafaib discover] year tabs: {year_order}", flush=True)

    # Extract all AIB/GAIB report anchor entries
    # Pattern: <a href="...">DD MON YY, CMD, AIRCRAFT, LOCATION, AIB REPORT</a>
    all_anchors = re.findall(r'<a\s+href=[\"\'](.*?)[\"\'](.*?)>(.*?)</a>', html, re.DOTALL)

    inserted = 0
    seen_case_ids = set()

    for raw_href, _attrs, text_raw in all_anchors:
        text = re.sub(r'\s+', ' ', text_raw).strip()

        # Must look like a report entry
        if not re.search(r'\bAIB\b|\bGAIB\b|Investigation Board', text, re.I):
            continue
        if re.search(r'PRESS RELEASE', text, re.I):
            continue
        if len(text) < 10:
            continue

        event_date, year, command, aircraft, location, rtype = parse_anchor_fields(text)

        # Build intrinsic case_id
        cid = build_case_id(event_date, aircraft, location)

        # Handle duplicates: some entries appear twice with slightly different text
        if cid in seen_case_ids:
            # Append disambiguator if truly same anchor
            existing = c.execute(
                "SELECT anchor_text FROM usafaib_reports WHERE case_id=?", (cid,)
            ).fetchone()
            if existing and existing['anchor_text'] == text:
                continue  # exact duplicate, skip
            # Different text -> append suffix
            cid = cid + "-2"
        seen_case_ids.add(cid)

        # Skip if already in DB
        if c.execute("SELECT 1 FROM usafaib_reports WHERE case_id=?", (cid,)).fetchone():
            continue

        # Normalize href
        report_url = raw_href if raw_href.startswith('http') else BASE_URL + raw_href
        if report_url.startswith(BASE_URL + 'Portals/'):
            report_url = BASE_URL + '/Portals/' + report_url[len(BASE_URL + 'Portals/'):]

        country = infer_country(location, text)

        c.execute(
            "INSERT OR IGNORE INTO usafaib_reports "
            "(case_id, year, report_url, raw_href, anchor_text, report_type, "
            " aircraft, operator, event_date, location, country, lang, "
            " status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, year, report_url, raw_href, text, rtype,
             aircraft, command, event_date, location, country, 'en',
             'new', now(), now()),
        )
        c.commit()
        inserted += 1

    print(f"[usafaib discover] inserted={inserted}", flush=True)
    return inserted

def fetch(c, cl):
    """Download PDFs for all status='new' rows.

    For LinkClick URLs: follow redirect in-stream to get final URL, then download.
    Files >60MB are skipped (large evidence packages).
    """
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, report_url, raw_href FROM usafaib_reports WHERE status='new'"
    ).fetchall()
    done = 0; toolarge = 0; fails = 0

    cl.headers["Referer"] = LISTING

    for row in rows:
        cid = row['case_id']
        raw_href = row['raw_href']
        report_url = row['report_url']

        # Resolve LinkClick URLs to direct PDF URLs first
        is_linkclick = 'LinkClick' in raw_href or 'fileticket=' in raw_href
        if is_linkclick:
            time.sleep(DELAY)
            resolved_url, size_hint = resolve_href(cl, raw_href)
            # Update resolved URL in DB
            c.execute("UPDATE usafaib_reports SET report_url=?,updated_at=? WHERE case_id=?",
                      (resolved_url, now(), cid))
            c.commit()
            pdf_url = resolved_url
        else:
            # Normalize direct URL
            if raw_href.startswith('/'):
                pdf_url = BASE_URL + raw_href
            elif raw_href.startswith('http'):
                pdf_url = raw_href
            else:
                pdf_url = BASE_URL + '/' + raw_href
            size_hint = -1

        # Check size with HEAD if not already known
        if size_hint < 0:
            try:
                hr = cl.head(pdf_url, headers={"Referer": LISTING})
                size_hint = int(hr.headers.get('content-length', -1))
            except Exception:
                size_hint = -1
            time.sleep(0.5)

        if size_hint > MAX_PDF_BYTES:
            print(f"[usafaib fetch] {cid}: SKIP too large ({size_hint//1024//1024}MB)", flush=True)
            c.execute("UPDATE usafaib_reports SET source_tier='toolarge',status='skipped',updated_at=? WHERE case_id=?",
                      (now(), cid))
            c.commit(); toolarge += 1; continue

        # Download
        dest = os.path.join(PDFDIR, re.sub(r'[^A-Za-z0-9_.-]', '_', cid) + '.pdf')
        try:
            time.sleep(DELAY)
            pdf_r = cl.get(pdf_url, headers={"Referer": LISTING})
            ct = pdf_r.headers.get('content-type', '')
            if 'pdf' not in ct.lower() and pdf_r.content[:4] != b'%PDF':
                raise ValueError(f"not a PDF (ct={ct} status={pdf_r.status_code})")
            with open(dest, 'wb') as fh:
                fh.write(pdf_r.content)
            c.execute(
                "UPDATE usafaib_reports SET pdf_path=?,status='fetched',report_url=?,updated_at=? WHERE case_id=?",
                (dest, pdf_url, now(), cid),
            )
            c.commit()
            done += 1; fails = 0
            print(f"[usafaib fetch] {cid}: {len(pdf_r.content)//1024}KB", flush=True)
        except Exception as e:
            print(f"[usafaib fetch] {cid}: FAIL {e}", file=sys.stderr, flush=True)
            fails += 1
            if fails >= 5:
                print("[usafaib fetch] 5 consecutive failures, aborting", file=sys.stderr)
                break

    print(f"[usafaib fetch] done={done} toolarge={toolarge} fails={fails}", flush=True)
    return done

def _parse_one(cid, pdf_path, use_ocr=False):
    """Parse a single PDF. Returns (narrative_text, registration, event_date, probable_cause, tier)."""
    txt = extract_text(pdf_path)

    if len(txt) < FLOOR:
        if use_ocr and pdf_path and os.path.exists(pdf_path):
            ocr_txt = ocr_extract(pdf_path, lang=OCR_LANG)
            if len(ocr_txt) >= FLOOR:
                print(f"[usafaib parse] {cid}: OCR yielded {len(ocr_txt)} chars", flush=True)
                txt = ocr_txt
                tier = 'ocr'
            else:
                print(f"[usafaib parse] {cid}: OCR also blank ({len(ocr_txt)} chars)", file=sys.stderr)
                return txt, None, None, None, 'scanned', None
        else:
            return txt, None, None, None, 'scanned', None
    else:
        tier = 'pdf'

    registration, event_date, probable_cause, aircraft_from_pdf = parse_report_text(txt)
    narrative = strip_boilerplate(txt)
    return narrative, registration, event_date, probable_cause, tier, aircraft_from_pdf

def parse(c):
    """Parse all status='fetched' rows."""
    rows = c.execute(
        "SELECT case_id, pdf_path FROM usafaib_reports WHERE status='fetched'"
    ).fetchall()
    scanned = 0
    for row in rows:
        narrative, reg, ev_date, prob_cause, tier, acft_pdf = _parse_one(
            row['case_id'], row['pdf_path'], use_ocr=True
        )
        # aircraft: prefer PDF-extracted if anchor yielded a base name not an aircraft
        c.execute(
            "UPDATE usafaib_reports SET "
            "narrative_text=?, registration=?, "
            "event_date=COALESCE(?,event_date), "
            "aircraft=CASE WHEN ? IS NOT NULL THEN ? ELSE aircraft END, "
            "probable_cause=?, source_tier=?, status='parsed', updated_at=? "
            "WHERE case_id=?",
            (narrative, reg, ev_date, acft_pdf, acft_pdf, prob_cause, tier, now(), row['case_id']),
        )
        c.commit()
        if tier == 'scanned':
            scanned += 1
    print(f"[usafaib parse] parsed={len(rows)} scanned={scanned}", flush=True)
    return len(rows)

def parse_skipped(c):
    """Re-run OCR on status='skipped' rows that have pdf_path (scanned PDFs)."""
    rows = c.execute(
        "SELECT case_id, pdf_path FROM usafaib_reports "
        "WHERE status='skipped' AND source_tier='scanned' AND pdf_path IS NOT NULL"
    ).fetchall()
    ocr_ok = 0; still_blank = 0
    for row in rows:
        cid = row['case_id']
        if not row['pdf_path'] or not os.path.exists(row['pdf_path']):
            continue
        narrative, reg, ev_date, prob_cause, tier, acft_pdf = _parse_one(cid, row['pdf_path'], use_ocr=True)
        if len(narrative or '') >= FLOOR:
            ocr_ok += 1
        else:
            still_blank += 1
        c.execute(
            "UPDATE usafaib_reports SET "
            "narrative_text=?, registration=?, event_date=COALESCE(?,event_date), "
            "aircraft=CASE WHEN ? IS NOT NULL THEN ? ELSE aircraft END, "
            "probable_cause=?, source_tier=?, status='parsed', updated_at=? "
            "WHERE case_id=?",
            (narrative, reg, ev_date, acft_pdf, acft_pdf, prob_cause, tier, now(), cid),
        )
        c.commit()
    print(f"[usafaib parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank

def build(c):
    """Build usafaib_accidents from all status='parsed' rows."""
    rows = c.execute(
        "SELECT case_id, event_date, aircraft, registration, operator, location, country, "
        "       narrative_text, probable_cause, report_url, report_type, source_tier "
        # GAIB ground mishaps (vehicle/fire/swim/fitness/parachute fatalities)
        # are NOT aviation accidents — excluded from the accidents projection
        # durably (the weekly cycle must never resurrect them).
        "FROM usafaib_reports WHERE status='parsed' AND report_type != 'GAIB report'"
    ).fetchall()
    built = 0; skipped_scanned = 0

    for r in rows:
        narr = r['narrative_text'] or ''
        tier = r['source_tier'] or ''

        if len(narr) < FLOOR or tier not in ('pdf', 'ocr'):
            skipped_scanned += 1
            c.execute("UPDATE usafaib_reports SET status='skipped',updated_at=? WHERE case_id=?",
                      (now(), r['case_id']))
            c.commit()
            continue

        operator = r['operator'] or 'U.S. Air Force'
        # Expand short command codes to readable names
        cmd_map = {
            'ACC': 'Air Combat Command (ACC)',
            'AMC': 'Air Mobility Command (AMC)',
            'AETC': 'Air Education and Training Command (AETC)',
            'AFGSC': 'Air Force Global Strike Command (AFGSC)',
            'AFMC': 'Air Force Materiel Command (AFMC)',
            'AFSC': 'Air Force Space Command',
            'AFSOC': 'Air Force Special Operations Command (AFSOC)',
            'PACAF': 'Pacific Air Forces (PACAF)',
            'USAFE': 'U.S. Air Forces in Europe (USAFE)',
        }
        operator_full = cmd_map.get((r['operator'] or '').upper(), operator)

        aircraft = r['aircraft']
        location = r['location']
        event_date = r['event_date']
        registration = r['registration']
        country = r['country'] or 'US'

        slug = site_slug(
            (event_date or '').replace('-', '')[:8],
            re.sub(r'[^A-Za-z0-9]+', '-', aircraft or 'unknown').lower()[:15],
            re.sub(r'[^A-Za-z0-9]+', '-', location or 'unknown').lower()[:20],
        )

        c.execute(
            "INSERT OR REPLACE INTO usafaib_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            " narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r['case_id'], event_date, aircraft, registration, operator_full,
             location, country, narr, r['probable_cause'], r['report_url'],
             r['report_type'] or 'AIB report', slug, 'en', now()),
        )
        c.execute("UPDATE usafaib_reports SET status='built',updated_at=? WHERE case_id=?",
                  (now(), r['case_id']))
        c.commit()
        built += 1

    print(f"[usafaib build] built={built} skipped_scanned={skipped_scanned}", flush=True)
    return built, skipped_scanned

def stats(c):
    """Print summary statistics."""
    print("\n=== usafaib_reports status breakdown ===")
    for row in c.execute("SELECT status, source_tier, count(*) n FROM usafaib_reports GROUP BY status, source_tier ORDER BY n DESC"):
        print(f"  status={row['status']}  tier={row['source_tier']}  n={row['n']}")
    total_acc = c.execute("SELECT count(*) FROM usafaib_accidents").fetchone()[0]
    print(f"usafaib_accidents: {total_acc}")
    if total_acc > 0:
        lengths = [r[0] for r in c.execute("SELECT length(narrative_text) FROM usafaib_accidents WHERE narrative_text IS NOT NULL").fetchall()]
        lengths.sort()
        mid = len(lengths) // 2
        print(f"  narrative len: min={min(lengths)} median={lengths[mid]} max={max(lengths)}")
        print("\nSample rows (case_id, date, aircraft, registration, slug, narr_len):")
        for row in c.execute(
            "SELECT case_id, event_date, aircraft, registration, site_slug, length(narrative_text) len "
            "FROM usafaib_accidents ORDER BY event_date DESC LIMIT 10"
        ):
            print(f"  {row['case_id'][:50]}  |  {row['event_date']}  |  {row['aircraft']}  |  "
                  f"{row['registration']}  |  {row['site_slug']}  |  narr={row['len']}")

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "fetch", "all"):
        cl = http()
        try:
            if mode in ("discover", "all"):
                print("discovered:", discover(c, cl))
            if mode in ("fetch", "all"):
                print("fetched:", fetch(c, cl))
        finally:
            cl.close()

    if mode in ("parse", "all"):
        print("parsed:", parse(c))

    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}")

    if mode in ("build", "all", "parse-skipped"):
        b, sc = build(c)
        print(f"built: {b}  skipped_scanned: {sc}")

    if mode in ("stats", "all"):
        stats(c)

    # Always show quick status
    print("\nreports status:")
    for row in c.execute("SELECT status, count(*) n FROM usafaib_reports GROUP BY status ORDER BY n DESC"):
        print(f"  {row[0]}: {row[1]}")
    print("accidents:", c.execute("SELECT count(*) FROM usafaib_accidents").fetchone()[0])

if __name__ == "__main__":
    main()
