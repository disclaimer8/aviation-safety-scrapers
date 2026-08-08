#!/usr/bin/env python3
"""sibpk — Pakistan SIB/BASI/AAIB aviation-accident ingest.

Source: aviation.gov.pk (Ministry of Aviation, formerly PCAA SIB / AAIB / BASI).
The live site times out from EU/DE IPs. All PDFs are fetched from the Wayback Machine
via the id_ raw-bytes flag.  Live aviation.gov.pk is also tried as fallback (some
2024–2025 reports are not in Wayback yet but Wayback thumbnails indicate existence).

PDF listing: The site never published a machine-readable index. URLs were discovered
via CDX API scans of aviation.gov.pk and the mirror at mod.gov.pk/SiteImage/Misc/files/.
The hardcoded REPORTS list below is the authoritative seed derived from that scan
(2026-06-09 recon). Recheck mode re-CDX-checks for new entries.

Body organisation (pre-2024): flat /SiteImage/Misc/files/NNNsib-*.pdf filenames
  with prefixes like 22SIB-307, ffSIB-AP-BHM, ddSIB-40-118, pSIB-417, 8AAIB-431 …
Post-2024 reorg: /SiteImage/Misc/files/reports 2024,2022/{YYYY}/*.pdf
  AND /SiteImage/Misc/files/reports 2024,2022/new files/(X) Final Report {FLTNO}.pdf

case_id format:  sibpk-{slug}   where slug is derived from the filename
  (stable + unique; registration-based where possible from filename).

Country: PK (all reports are Pakistan-jurisdiction).
Registration prefix: AP-.

Stages: discover | fetch | parse | build | recheck | all  (via argv[1])
  all = discover + fetch + parse + build in sequence.
  recheck = only probe for new PDFs not in DB.

Wayback politeness: 2 s base delay; backoff on 429/503.
pdftotext floor: 80 chars; scanned PDFs → skip with OCR-TODO note.
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, tempfile, hashlib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOME   = os.path.expanduser("~/sibpk-ingest")
DB     = os.path.join(HOME, "sibpk.db")
PDFDIR = os.path.join(HOME, "pdfs")
LOG    = os.path.join(HOME, "fetch.log")

COUNTRY = "PK"
LANG    = "en"
FLOOR   = 80   # min chars to consider text usable

# Authority base URL (used for source_url)
LIVE_BASE = "https://aviation.gov.pk"
WAYBACK   = "https://web.archive.org/web"
CDX_API   = "https://web.archive.org/cdx/search/cdx"
# BASIP "Final Investigation Report" listing on the live site. The id is a
# base64 GUID and has been stable; if it 404s, re-derive it from the homepage
# (anchor text "BASIP - Final Investigation Report").
LISTING_URL = LIVE_BASE + "/Detail/NTc2ZDUxZDMtNjY1NC00YTM3LTgzNTYtNzA4ZGFiMjBiZmNl"
DELAY     = 2.0   # seconds between Wayback requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# ---------------------------------------------------------------------------
# REPORTS seed — derived from CDX scan 2026-06-09
# Each entry: (year_hint, filename, wayback_ts, live_path)
# year_hint: approximate accident year (from directory name or filename analysis)
# wayback_ts: best snapshot timestamp from CDX (use "" to auto-discover)
# live_path: path under aviation.gov.pk (URL-encoded)
# ---------------------------------------------------------------------------
REPORTS = [
    # --- Old flat namespace (2006–2020): uploaded to /SiteImage/Misc/files/ directly ---
    # Reorg copies (2024 reupload to /reports 2024,2022/) are preferred when both exist.
    # 2006
    ("2006", "22SIB-307", "20230605101014",
     "/SiteImage/Misc/files/22SIB-307.pdf"),
    # 2010
    ("2010", "23SIB-337", "20230201225439",
     "/SiteImage/Misc/files/23SIB-337.pdf"),
    ("2010", "9SIB-IL-76", "20230605104052",
     "/SiteImage/Misc/files/9SIB-IL-76.pdf"),
    ("2010", "oJS-Report", "20230605090517",
     "/SiteImage/Misc/files/oJS-Report.pdf"),
    # 2012
    ("2012", "21SIB-350", "20230201230022",
     "/SiteImage/Misc/files/21SIB-350.pdf"),
    # 2014
    ("2014", "364RFC-AP-BEJ", "20230605114722",
     "/SiteImage/Misc/files/364RFC%20AP-BEJ.pdf"),
    # 2015
    ("2015", "374SIB-SAI-AP-BJO", "20230201211958",
     "/SiteImage/Misc/files/374SIB-SAI-AP-BJO.pdf"),
    # 2016
    ("2016", "2-AAIB-386", "20230201231634",
     "/SiteImage/Misc/files/2_AAIB-386.pdf"),
    ("2016", "6B737-200-AP-BKE", "20211228231133",
     "/SiteImage/Misc/files/6B737-200%20AP-BKE.pdf"),
    ("2016", "17SIB-AP-BII", "20230605104005",
     "/SiteImage/Misc/files/17SIB-AP-BII.pdf"),
    # 2017
    ("2017", "14SIB-398", "20230201222038",
     "/SiteImage/Misc/files/14SIB-398.pdf"),
    ("2017", "15SIB-397", "20230605104450",
     "/SiteImage/Misc/files/15SIB-397.pdf"),
    ("2017", "16SIB-AP-ZBQ", "20230201213719",
     "/SiteImage/Misc/files/16SIB-AP-ZBQ.pdf"),
    ("2017", "ffSIB-AP-BHM-ATR42-500", "20230605104212",
     "/SiteImage/Misc/files/ffSIB-AP-BHM_ATR42-500.pdf"),
    ("2017", "Final-Report-AP-BKZ-PIA588", "20240705150026",
     "/SiteImage/Misc/files/Final_Report_AP-BKZ%2c_PIA_588_occurred_on_25-12-2017_(Public_Version)%5b1%5d.pdf"),
    ("2017", "SIB-399", "20251117065057",
     "/SiteImage/Misc/files/reports%202024%2C2022/2017/SIB-399.pdf"),
    ("2017", "TCAS-RA-ABQ-200", "20251117202133",
     "/SiteImage/Misc/files/reports%202024%2C2022/2017/uploading%20TCAS-RA%20Final%20Investigation%20Report%20of%20%20Airblue%20%20ABQ-200.pdf"),
    # 2018
    ("2018", "13SIB-414", "20230605095007",
     "/SiteImage/Misc/files/13SIB-414.pdf"),
    ("2018", "7AAIB-40-130", "20230605104249",
     "/SiteImage/Misc/files/7AAIB-40-130.pdf"),
    ("2018", "AAIB-40-129", "20230201213355",
     "/SiteImage/Misc/files/AAIB-40-129.pdf"),
    ("2018", "ddSIB-40-118", "20230605091631",
     "/SiteImage/Misc/files/ddSIB-40-118.pdf"),
    ("2018", "pSIB-417-New", "20230605094842",
     "/SiteImage/Misc/files/pSIB-417%20New.pdf"),
    ("2018", "ABQ675-A321-AP-BMW", "20251117075000",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/(Public_Version)_Final_Investigation_Report_of_ABQ675_A321_Reg__No__AP-BMW_on_22-02-2018%5B1%5D.pdf"),
    ("2018", "ABY555-A320-A6ANL", "20251117080848",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/(Public_Version)_Final_Investigation_Report_of_ABY-555_A320_REG_NO_A6ANL_ON_06-06-2018_0001%5B1%5D.pdf"),
    ("2018", "PK517-ATR72-AP-BKY-Panjgur", "20251117071748",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/Serious_Incident_PIA_PK517%2C_ATR_72_AP-BKY_at_Panjgur_Airport_on_10_Nov_2018_Public_Version%5B1%5D.pdf"),
    ("2018", "SIB-40-133", "20251117081913",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/SIB-40-133.pdf"),
    ("2018", "8AAIB-431", "20210927032154",
     "/SiteImage/Misc/files/8AAIB-431.pdf"),
    ("2018", "UAE615-Emirates", "20240724053647",
     "/SiteImage/Misc/files/Final_Report_Emirates_UAE615_A6_END_27-02-2018%5b1%5d.pdf"),
    ("2018", "PIA398-lightning-AP-BLU", "20260202122503",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/Investigation_Report_-_Lightening_Strike_Case_PIA-398%2C_AP-BLU_on_12-02-18%5B1%5D.pdf"),
    ("2018", "PIA585-TCAS", "20260202153725",
     "/SiteImage/Misc/files/reports%202024%2C2022/2018/uploading%20TCAS-RA%20Final%20Investigation%20Report%20of%20%20PIA-585.pdf"),
    # 2019
    ("2019", "PIA605-ATR42-AP-BHP-Gilgit", "20241213041058",
     "/SiteImage/Misc/files/2024%20Files/Final%20Inv%20Report_PIA-605_ATR42_AP-BHP_20%20July%202019%20at%20Gilgit_Public%20Version_-compres.pdf"),
    ("2019", "UAE637", "20240706041623",
     "/SiteImage/Misc/files/Final%20Report%20of%20UAE-637(1).pdf"),
    ("2019", "AP-BIX-Cessna172-LFC", "20260202000000",
     "/SiteImage/Misc/files/reports%202024%2C2022/2019/Investigation_Report_-_Lahore_Flying_Club%2C_AP-BIX%2C_Cessna_172_on_04-04-2019%5B1%5D.pdf"),
    # 2020
    ("2020", "PIA8303-A320-AP-BLD-Karachi-final", "20240724053623",
     "/SiteImage/Misc/files/(PUBLIC%20VERSION)%20FINAL%20INVESTIGATION%20REPORT%20PIA%208303%20AP-BLD(1).pdf"),
    ("2020", "PIA8303-interim-statement", "20230201224654",
     "/SiteImage/Misc/files/1_AAIB%20Interim%20Statement%20Accident%20of%20PIA%20A320%20AP-BLD%20Near%20Karachi%20Airport%20on%2022-05-2020.pdf"),
    ("2020", "AAIB-40-135", "20230605093842",
     "/SiteImage/Misc/files/3_AAIB-40-135.pdf"),
    ("2020", "AP-BLD-interim-2022", "20230605114734",
     "/SiteImage/Misc/files/Interim%20Statement%20AP-BLD%20for%20%20Aviation%20Division%202022(1).pdf"),
    # New batch (2021–2023 accidents, published via 'new files' reorg)
    ("2021", "ANK4506", "20260202123742",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(a)%20Final%20Report%20ANK4506.pdf"),
    ("2021", "ABY531", "20260202140046",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(b)%20Final%20Report%20ABY531.pdf"),
    ("2021", "ABQ611", "20260202074313",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(c)%20Final%20Report%20ABQ611.pdf"),
    ("2021", "KAC920", "20260202124508",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(d)%20Final%20Report%20KAC920.pdf"),
    ("2022", "PIA303", "20260202145619",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(e)%20Final%20Report%20PIA303.pdf"),
    ("2022", "UAE600", "20260202131951",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(g)%20Final%20Report%20UAE600.pdf"),
    ("2022", "ABQ613", "20250918204429",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(h)%20Final%20Report%20ABQ613.pdf"),
    ("2022", "GFA770", "20260202125836",
     "/SiteImage/Misc/files/reports%202024%2C2022/new%20files/(i)%20Final%20Report%20GFA770.pdf"),
    ("2022", "ER522-AP-BNB", "20240705115000",
     "/SiteImage/Misc/files/Final_Report_AP-BNB%2c_ER_522_occurred_on_02-03-2022%5b1%5d.pdf"),
    ("2022", "ABQ412-smoke-cargo", "20240705134200",
     "/SiteImage/Misc/files/SERIOUS_INCIDENT_(SMOKE_IN_AFT_CARGO)_AIRBLUE_FLIGHT_ABQ-412_(SECTOR_LHE-%E2%80%93_SHJ)_25_July_2022_Public_Version%5B1%5D.pdf"),
    ("2022", "Etihad-ETD72K", "20251001121703",
     "/SiteImage/Misc/files/reports%202024%2C2022/12-08-2025%20Final%20Etihad%20Airways%20ETD-72K%2027%20May%202022_Public%20Version.pdf"),
    # 2023
    ("2023", "PIA743-B777-AP-BMH", "20260202144535",
     "/SiteImage/Misc/files/Final%20Inv%20Report_PIA%20PK-743_B777_AP-BMH%20on%202-12-2023%20(1).pdf"),
    ("2023", "CSN5237", "20250404234624",
     "/SiteImage/Misc/files/Aviation%20Documents/09-10-24%20Public%20Final%20Report%20CSN5237.pdf"),
    # 2024/2025
    ("2024", "SVA724-wrong-runway", "20260202143122",
     "/SiteImage/Misc/files/Aviation%20Documents/SVA724_Public_Final_Report_-_Copy%5B1%5D(1).pdf"),
    ("2025", "PK-150", "20251111091320",
     "/SiteImage/Misc/files/4-11-25%20(Public%20Version%20V2)%20Final%20Investigation%20Report%20PK-150.pdf"),
    ("2025", "ABQ410", "20260202124240",
     "/SiteImage/Misc/files/21-10-25%20Final%20Report%20ABQ-410%20(Public%20Version).pdf"),
    ("2025", "SVA792-TCAS", "20260202125941",
     "/SiteImage/Misc/files/27-10-2025%20(PUBLIC%20VERSION)%20FINAL%20INVESTIGATION%20REPORT%20SVA792%20TCAS-RA%20-%20Copy.pdf"),
    ("2025", "ABQ211", "20260202131258",
     "/SiteImage/Misc/files/ABQ-211%20FINAL%20REPORT%20(Public%20Version).pdf"),
    ("2025", "AP-BLO-preliminary", "20260202122937",
     "/SiteImage/Misc/files/628---AP-BLO%20Ammended%20%20preliminary%20Investigation%20report%20final%20version%2017-10-25.pdf"),
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS sibpk_reports (
  case_id      TEXT PRIMARY KEY,
  source_url   TEXT,
  wayback_url  TEXT,
  wayback_ts   TEXT,
  pdf_path     TEXT,
  year_hint    TEXT,
  report_type  TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  aircraft     TEXT,
  registration TEXT,
  operator     TEXT,
  event_date   TEXT,
  location     TEXT,
  status       TEXT DEFAULT 'new',
  skip_reason  TEXT,
  discovered_at INT,
  updated_at   INT
);
CREATE TABLE IF NOT EXISTS sibpk_accidents (
  case_id       TEXT PRIMARY KEY,
  event_date    TEXT,
  aircraft      TEXT,
  registration  TEXT,
  operator      TEXT,
  location      TEXT,
  country       TEXT DEFAULT 'PK',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url    TEXT,
  report_type   TEXT,
  site_slug     TEXT,
  lang          TEXT DEFAULT 'en',
  built_at      INT
);
CREATE INDEX IF NOT EXISTS idx_sibpk_status ON sibpk_reports(status);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
import httpx

def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def _get(url, timeout=35, retries=3):
    """Fetch URL with politeness and backoff. Returns httpx.Response or None."""
    headers = {"User-Agent": UA}
    for attempt in range(retries):
        try:
            r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if r.status_code in (429, 503):
                wait = 30 * (2 ** attempt)
                _log(f"  rate-limited ({r.status_code}), sleeping {wait}s")
                time.sleep(wait)
                continue
            return r
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt == retries - 1:
                _log(f"  fetch failed: {e}")
                return None
            time.sleep(10)
    return None

def _wayback_url(live_path, ts):
    """Construct Wayback id_ URL for raw PDF bytes."""
    live = LIVE_BASE + live_path
    return f"{WAYBACK}/{ts}id_/{live}"

def _slug_from_name(name):
    """Convert report name to safe slug."""
    s = re.sub(r'[^a-zA-Z0-9\-]', '-', name)
    s = re.sub(r'-+', '-', s).strip('-').lower()
    return s

def _case_id(name):
    return "sibpk-" + _slug_from_name(name)

def _pdftotext(pdf_bytes):
    """Extract text from PDF bytes. Returns (text, is_scanned)."""
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(pdf_bytes)
        fname = f.name
    try:
        result = subprocess.run(
            ['pdftotext', '-q', fname, '-'],
            capture_output=True, text=True, timeout=60
        )
        text = result.stdout.strip()
        return text, len(text) < FLOOR
    except Exception as e:
        return "", True
    finally:
        try:
            os.unlink(fname)
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------
EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_DATE_PATTERNS = [
    re.compile(r'\b(\d{1,2})\s+(' + '|'.join(EN_MONTHS) + r')\s+(\d{4})\b', re.I),
    re.compile(r'\b(' + '|'.join(EN_MONTHS) + r')\s+(\d{1,2})[,\s]+(\d{4})\b', re.I),
    re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b'),
    re.compile(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})\b'),
    re.compile(r'\b(\d{1,2})[./\-](\d{1,2})[./\-](\d{2})\b'),
]

def _extract_date(text, year_hint):
    """Extract event date from report text. Returns ISO string or None."""
    # Try structured date patterns
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text[:3000]):
            g = m.groups()
            try:
                if len(g) == 3:
                    if pat.pattern.startswith(r'\b(\d{4})'):
                        # YYYY-MM-DD
                        y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    elif pat.pattern.startswith(r'\b(\d{1,2})\s+('):
                        # D Month YYYY
                        d, month_str, y = int(g[0]), g[1].lower(), int(g[2])
                        mo = EN_MONTHS.get(month_str[:3], 0)
                        if not mo:
                            for k, v in EN_MONTHS.items():
                                if month_str.lower().startswith(k[:3]):
                                    mo = v
                                    break
                    elif any(pat.pattern.startswith(f'\\b({k}') for k in ['(jan', '(feb', '(mar', '(apr', '(may', '(jun', '(jul', '(aug', '(sep', '(oct', '(nov', '(dec']):
                        month_str, d, y = g[0].lower(), int(g[1]), int(g[2])
                        mo = EN_MONTHS.get(month_str[:3], 0)
                    else:
                        # D/M/YYYY or D/M/YY
                        d, mo, y = int(g[0]), int(g[1]), int(g[2])
                        if y < 100:
                            y += 2000 if y < 50 else 1900
                    if 1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2030:
                        return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, TypeError):
                continue
    # Fallback: year only from year_hint
    if year_hint and re.match(r'\d{4}', year_hint):
        return year_hint + "-01-01"
    return None

def _extract_registration(text):
    """Extract Pakistani registration (AP-XXX) from text."""
    m = re.search(r'\bAP-([A-Z]{2,3})\b', text[:2000])
    if m:
        return "AP-" + m.group(1)
    # Also catch non-Pakistani regs for foreign aircraft involved
    m2 = re.search(r'\b([A-Z]{1,2}-[A-Z]{3,4}|[A-Z]\d[A-Z]{3})\b', text[:1000])
    if m2:
        return m2.group(0)
    return None

def _extract_operator(text):
    """Extract operator/airline name."""
    patterns = [
        r'(?:Operator|Airline|Carrier)\s*[:\-]\s*([^\n\r]{3,60})',
        r'(?:PIA|Pakistan International Airline|Airblue|Serene Air|Air Arabia|AirBlue)',
        r'M/S\s+([A-Z][^\n\r]{3,50})',
    ]
    for pat in patterns:
        m = re.search(pat, text[:3000], re.I)
        if m:
            try:
                val = m.group(1).strip() if m.lastindex and m.lastindex >= 1 else m.group(0).strip()
            except IndexError:
                val = m.group(0).strip()
            return val[:100]
    return None

def _extract_aircraft(text, name):
    """Extract aircraft type."""
    # Common types
    types = [
        'ATR-72', 'ATR-42', 'ATR 72', 'ATR 42',
        'A320', 'A319', 'A321', 'A330', 'A310',
        'B737', 'B777', 'B767', 'B747', 'B757',
        '737-200', '737-300', '737-400', '737-500', '737-800',
        'F-27', 'Fokker 27', 'Fokker F27',
        'IL-76', 'C-130',
        'Cessna 172', 'Cessna-172',
        'Mi-8', 'MI-8', 'Mi-17',
    ]
    lo = text[:3000]
    for t in types:
        if t.lower() in lo.lower():
            return t
    # From filename
    for t in types:
        if t.replace('-','').replace(' ','').lower() in name.lower():
            return t
    m = re.search(r'\b(ATR[\-\s]\d+|A\d{3}|B\d{3}|Boeing\s+\d+|Airbus\s+A\d+|F-\d+)\b', lo)
    if m:
        return m.group(0)
    return None

def _extract_location(text):
    """Extract accident location."""
    # Common Pakistan cities/airports
    cities = ['Karachi', 'Lahore', 'Islamabad', 'Rawalpindi', 'Peshawar', 'Quetta',
              'Multan', 'Havelian', 'Gilgit', 'Panjgur', 'Chitral', 'Turbat',
              'Abbottabad', 'Faisalabad', 'Sialkot', 'Nawabshah']
    for city in cities:
        if city.lower() in text[:3000].lower():
            m = re.search(
                r'(?:at|near|of|Airport)\s+(' + city + r'[^\n\r,]{0,30})',
                text[:3000], re.I
            )
            if m:
                return m.group(1).strip()
            return city
    m = re.search(r'(?:at|near)\s+([A-Z][A-Za-z\s]{3,40}(?:Airport|Airfield|aerodrome)?)',
                  text[:2000])
    if m:
        return m.group(1).strip()
    return None

def _extract_probable_cause(text):
    """Extract probable cause section."""
    patterns = [
        r'(?:Probable\s+Cause[s]?|Primary\s+Cause|Cause\s+of\s+Accident)\s*[:\n]+(.{50,2000}?)(?:\n\s*\n|\Z)',
        r'(?:PROBABLE\s+CAUSE[S]?|PRIMARY\s+CAUSE)\s*[:\n]+(.{50,2000}?)(?:\n\s*\n|\Z)',
        r'(?:Finding[s]?\s+and\s+Conclusion[s]?|FINDINGS?)\s*[:\n]+(.{50,2000}?)(?:\n\s*\n|\Z)',
        r'(?:Conclusion[s]?|CONCLUSION[S]?)\s*[:\n]+(.{50,2000}?)(?:\n\s*\n|\Z)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL | re.I)
        if m:
            pc = m.group(1).strip()
            # Trim at next section header
            pc = re.split(r'\n(?:[A-Z][A-Z\s]{3,}:|\d+\.)', pc)[0].strip()
            if len(pc) > 50:
                return pc[:2000]
    return None

def _report_type(name, text):
    """Classify as final/interim/preliminary/serious_incident."""
    lo_name = name.lower()
    lo_text = text[:500].lower() if text else ''
    if 'interim' in lo_name or 'interim' in lo_text:
        return 'interim'
    if 'preliminary' in lo_name or 'preliminary' in lo_text:
        return 'preliminary'
    if 'serious' in lo_name or 'serious incident' in lo_text:
        return 'serious_incident'
    return 'final'

def _site_slug(case_id, reg):
    """Generate site slug from case_id (always unique) with reg hint."""
    # case_id is already sibpk-... and guaranteed unique — use it as slug
    # Optionally include registration prefix for readability
    name = case_id.replace('sibpk-', '')[:30]
    return case_id  # case_id IS the slug — always unique

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db_connect():
    os.makedirs(HOME, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con

# ---------------------------------------------------------------------------
# Stage: discover
# ---------------------------------------------------------------------------
def _discover_live():
    """Scrape the BASIP listing for PDF links. Returns [(name, live_path)].

    Returns [] on any failure — a listing outage must degrade to "seed only",
    never abort the cycle. The page is plain server-rendered HTML; no anti-bot
    was present when this was written (measured from the minipc, 2026-08-03).
    """
    r = _get(LISTING_URL, timeout=45)
    if not r or r.status_code != 200:
        _log(f"DISCOVER-LIVE: listing unreachable (HTTP {getattr(r, 'status_code', 'n/a')}) — seed only")
        return []
    found = []
    seen = set()
    for m in re.finditer(r'href="([^"]+\.pdf)"', r.text, re.I):
        path = m.group(1)
        if not path.startswith('/'):
            path = '/' + path.lstrip('./')
        if path in seen:
            continue
        seen.add(path)
        # Filenames are percent-encoded in the href and may sit in a
        # subdirectory ("Aviation Documents/..."), which the original flat
        # /SiteImage/Misc/files/ assumption did not cover.
        name = urllib.parse.unquote(path.rsplit('/', 1)[-1])
        found.append((name, path))
    _log(f"DISCOVER-LIVE: {len(found)} PDFs on the listing")
    return found


def _year_from_name(name):
    """Best-effort year hint from the filename. Returns a 4-digit STRING, or ''
    when nothing is recoverable.

    Must be '' and never 0: year_hint is a TEXT column, so SQLite stores 0 as
    '0', and stage_build's fallback (`row['year_hint'] + '-01-01'`) sees a
    non-empty string and emits the bogus date '0-01-01'. That is exactly what
    happened on the first live-discovery build — 5 of 97 rows.
    """
    m = re.search(r'(?:19|20)\d{2}', name)
    if m:
        return m.group(0)
    # DD-MMM-YY / DD-MM-YY (e.g. "11-apr-18", "27-10-25") — a real year we
    # would otherwise throw away. Same 50-year pivot the D/M/YY parser uses.
    m = re.search(r'\b\d{1,2}[-/](?:\d{1,2}|[A-Za-z]{3,})[-/](\d{2})\b', name)
    if m:
        yy = int(m.group(1))
        return str(2000 + yy if yy < 50 else 1900 + yy)
    return ''


def stage_discover(con):
    """Seed all known reports into sibpk_reports table."""
    _log(f"DISCOVER: seeding {len(REPORTS)} known reports")
    cur = con.cursor()
    now = int(time.time())
    inserted = 0
    for year_hint, name, wayback_ts, live_path in REPORTS:
        cid = _case_id(name)
        source_url = LIVE_BASE + live_path
        wb_url = _wayback_url(live_path, wayback_ts) if wayback_ts else ""
        existing = cur.execute("SELECT 1 FROM sibpk_reports WHERE case_id=?", (cid,)).fetchone()
        if not existing:
            cur.execute("""
                INSERT INTO sibpk_reports
                  (case_id, source_url, wayback_url, wayback_ts, year_hint, status, discovered_at, updated_at)
                VALUES (?,?,?,?,?,'new',?,?)
            """, (cid, source_url, wb_url, wayback_ts, year_hint, now, now))
            inserted += 1
    con.commit()
    _log(f"DISCOVER: {inserted} new from seed, {len(REPORTS)-inserted} already present")

    # Live listing pass — ADDITIVE. The seed stays authoritative for the 30
    # reports that have scrolled off the current page; this only adds what the
    # site shows today and we do not already hold.
    #
    # Dedup on source_url, NOT case_id. Seed rows are keyed by a CURATED short
    # slug (sibpk-sib-399, sibpk-abq675-a321-ap-bmw) taken from the report's
    # identity, while a live row can only be keyed off its filename — the two
    # never collide, so a case_id check would re-insert all 70 as duplicates
    # (measured: it did). source_url is the canonical live URL and matches
    # byte-for-byte across both paths.
    live_new = 0
    for name, live_path in _discover_live():
        surl = LIVE_BASE + live_path
        if cur.execute("SELECT 1 FROM sibpk_reports WHERE source_url=?", (surl,)).fetchone():
            continue
        cid = _case_id(name)
        if cur.execute("SELECT 1 FROM sibpk_reports WHERE case_id=?", (cid,)).fetchone():
            continue
        cur.execute("""
            INSERT INTO sibpk_reports
              (case_id, source_url, wayback_url, wayback_ts, year_hint, status, discovered_at, updated_at)
            VALUES (?,?,'','',?,'new',?,?)
        """, (cid, LIVE_BASE + live_path, _year_from_name(name), now, now))
        live_new += 1
    con.commit()
    _log(f"DISCOVER-LIVE: {live_new} new from live listing")

# ---------------------------------------------------------------------------
# Stage: fetch
# ---------------------------------------------------------------------------
def stage_fetch(con):
    """Download PDFs for all 'new' or 'retry' reports."""
    os.makedirs(PDFDIR, exist_ok=True)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT case_id, source_url, wayback_url, wayback_ts, year_hint "
        # 'truncated' = a Wayback capture cut at exactly 1 MB. Those are now
        # re-fetchable from the live site, so they re-enter the queue.
        "FROM sibpk_reports WHERE status IN ('new','retry','truncated') ORDER BY year_hint"
    ).fetchall()
    _log(f"FETCH: {len(rows)} reports to download")

    for row in rows:
        cid = row['case_id']
        wb_url = row['wayback_url']
        source_url = row['source_url']
        year_hint = row['year_hint']
        now = int(time.time())

        pdf_path = os.path.join(PDFDIR, cid + ".pdf")

        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000:
            # Already downloaded
            cur.execute("UPDATE sibpk_reports SET status='downloaded', updated_at=? WHERE case_id=?",
                        (now, cid))
            con.commit()
            continue

        # Try Wayback first
        fetch_url = wb_url
        fetched = False
        fetched_via = None

        if fetch_url:
            _log(f"  [{cid}] Wayback fetch: {fetch_url[-80:]}")
            r = _get(fetch_url, timeout=60)
            if r and r.status_code == 200 and r.content[:4] == b'%PDF':
                with open(pdf_path, 'wb') as f:
                    f.write(r.content)
                _log(f"  [{cid}] OK via Wayback ({len(r.content):,} bytes)")
                fetched = True
                fetched_via = 'wayback'
            time.sleep(DELAY)

        # Live site. Primary for rows discovered from the listing (no wayback_ts),
        # fallback for anything Wayback would not give us. The old code skipped
        # straight to 'not_archived' here on the 2026-06 premise that the live
        # host was unreachable — it answers 200 now, so not trying it threw away
        # every report Wayback had not captured.
        if not fetched and source_url:
            _log(f"  [{cid}] live fetch: {source_url[-80:]}")
            r = _get(source_url, timeout=60)
            if r and r.status_code == 200 and r.content[:4] == b'%PDF':
                with open(pdf_path, 'wb') as f:
                    f.write(r.content)
                _log(f"  [{cid}] OK via live site ({len(r.content):,} bytes)")
                fetched = True
                fetched_via = 'live'
            time.sleep(DELAY)

        if not fetched:
            _log(f"  [{cid}] neither Wayback nor live site returned a PDF")
            cur.execute(
                "UPDATE sibpk_reports SET status='not_archived', skip_reason='wayback_failed', updated_at=? WHERE case_id=?",
                (now, cid)
            )
            con.commit()
            continue

        size = os.path.getsize(pdf_path)
        # Check for Wayback 1MB truncation
        if size == 1048576 and fetched_via == 'wayback':
            _log(f"  [{cid}] WARNING: exactly 1MB — likely Wayback truncation; flagging")
            cur.execute(
                "UPDATE sibpk_reports SET status='truncated', skip_reason='wayback_1mb_limit', pdf_path=?, updated_at=? WHERE case_id=?",
                (pdf_path, now, cid)
            )
        else:
            cur.execute(
                "UPDATE sibpk_reports SET status='downloaded', pdf_path=?, updated_at=? WHERE case_id=?",
                (pdf_path, now, cid)
            )
        con.commit()

# ---------------------------------------------------------------------------
# Stage: parse
# ---------------------------------------------------------------------------
def stage_parse(con):
    """Extract text and metadata from downloaded PDFs."""
    cur = con.cursor()
    rows = cur.execute(
        "SELECT case_id, pdf_path, year_hint, source_url "
        "FROM sibpk_reports WHERE status IN ('downloaded','truncated') AND pdf_path IS NOT NULL"
    ).fetchall()
    _log(f"PARSE: {len(rows)} reports to parse")

    for row in rows:
        cid = row['case_id']
        pdf_path = row['pdf_path']
        year_hint = row['year_hint']
        now = int(time.time())

        if not os.path.exists(pdf_path):
            _log(f"  [{cid}] PDF missing: {pdf_path}")
            continue

        with open(pdf_path, 'rb') as f:
            pdf_bytes = f.read()

        text, is_scanned = _pdftotext(pdf_bytes)

        if is_scanned:
            _log(f"  [{cid}] scanned/empty ({len(text)} chars) → OCR-TODO")
            cur.execute(
                "UPDATE sibpk_reports SET status='ocr_todo', skip_reason='scanned_or_truncated', updated_at=? WHERE case_id=?",
                (now, cid)
            )
            con.commit()
            continue

        _log(f"  [{cid}] text OK ({len(text):,} chars)")

        # Extract metadata
        name = cid.replace('sibpk-', '')
        event_date = _extract_date(text, year_hint)
        registration = _extract_registration(text)
        aircraft = _extract_aircraft(text, name)
        operator = _extract_operator(text)
        location = _extract_location(text)
        probable_cause = _extract_probable_cause(text)
        rtype = _report_type(name, text)

        cur.execute("""
            UPDATE sibpk_reports SET
              narrative_text=?, probable_cause=?, aircraft=?, registration=?,
              operator=?, event_date=?, location=?, report_type=?,
              status='parsed', updated_at=?
            WHERE case_id=?
        """, (text, probable_cause, aircraft, registration,
              operator, event_date, location, rtype, now, cid))
        con.commit()

# ---------------------------------------------------------------------------
# Stage: build
# ---------------------------------------------------------------------------
def stage_build(con):
    """Populate sibpk_accidents from parsed reports."""
    cur = con.cursor()
    rows = cur.execute(
        "SELECT case_id, source_url, narrative_text, probable_cause, aircraft, "
        "registration, operator, event_date, location, report_type, year_hint "
        "FROM sibpk_reports WHERE status='parsed'"
    ).fetchall()
    _log(f"BUILD: {len(rows)} parsed reports → sibpk_accidents")

    now = int(time.time())
    built = 0
    for row in rows:
        cid = row['case_id']
        reg = row['registration'] or ''
        name = cid.replace('sibpk-', '')
        site_slug = _site_slug(name, reg)

        # event_date fallback to year_hint
        event_date = row['event_date'] or (row['year_hint'] + '-01-01' if row['year_hint'] else None)

        cur.execute("""
            INSERT OR REPLACE INTO sibpk_accidents
              (case_id, event_date, aircraft, registration, operator, location, country,
               narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cid, event_date, row['aircraft'], row['registration'], row['operator'],
            row['location'], COUNTRY, row['narrative_text'], row['probable_cause'],
            row['source_url'], row['report_type'], site_slug, LANG, now
        ))
        built += 1

    con.commit()
    _log(f"BUILD: {built} rows in sibpk_accidents")

# ---------------------------------------------------------------------------
# Stage: recheck
# ---------------------------------------------------------------------------
def stage_recheck(con):
    """Re-CDX-check for new PDFs not yet in DB. Adds any found to sibpk_reports."""
    _log("RECHECK: scanning CDX for new aviation.gov.pk SIB/BASI PDFs")
    import httpx

    patterns = [
        "aviation.gov.pk/SiteImage/Misc/files/*",
    ]
    existing_sources = set(
        r['source_url'] for r in
        con.execute("SELECT source_url FROM sibpk_reports").fetchall()
    )

    headers = {"User-Agent": UA}
    new_found = 0
    for pat in patterns:
        try:
            r = httpx.get(
                CDX_API,
                params={"url": pat, "output": "text", "fl": "original,statuscode,timestamp", "limit": "5000"},
                headers=headers, timeout=60
            )
            lines = r.text.strip().split('\n')
            for line in lines:
                parts = line.split(' ')
                if len(parts) < 3:
                    continue
                url, status, ts = parts[0], parts[1], parts[2]
                if status != '200' or '.pdf' not in url.lower():
                    continue
                # Filter investigation-related
                lo = url.lower()
                if not any(k in lo for k in ['sib', 'aaib', 'basi', 'investigation', 'report',
                                               'accident', 'ap-', 'pia', 'atr', 'a320', 'final',
                                               'public', 'interim', 'preliminary', 'abq', 'aby',
                                               'ank', 'kac', 'uae', 'csn', 'svn', 'etd', 'gfa']):
                    continue
                if url in existing_sources:
                    continue
                # Parse path from URL
                live_path = '/' + url.split('/', 3)[-1] if '/' in url else url
                name = os.path.basename(urllib.parse.unquote(live_path))[:60]
                name = re.sub(r'\.pdf$', '', name, flags=re.I)
                cid = _case_id(name)
                now = int(time.time())
                con.execute("""
                    INSERT OR IGNORE INTO sibpk_reports
                      (case_id, source_url, wayback_url, wayback_ts, year_hint, status, discovered_at, updated_at)
                    VALUES (?,?,?,?,'unknown','new',?,?)
                """, (cid, url, _wayback_url(live_path, ts), ts, now, now))
                new_found += 1
                _log(f"  RECHECK new: {cid}")
            time.sleep(DELAY)
        except Exception as e:
            _log(f"  RECHECK CDX error: {e}")

    con.commit()
    _log(f"RECHECK: {new_found} new reports added")

# ---------------------------------------------------------------------------
# Stats / dump
# ---------------------------------------------------------------------------
def stage_stats(con):
    """Print summary statistics."""
    print("\n=== sibpk DB stats ===")
    for row in con.execute("SELECT status, count(*) as n FROM sibpk_reports GROUP BY status ORDER BY n DESC"):
        print(f"  sibpk_reports.{row['status']}: {row['n']}")
    total = con.execute("SELECT count(*) FROM sibpk_accidents").fetchone()[0]
    print(f"  sibpk_accidents total: {total}")
    print("\n=== sibpk_accidents dump ===")
    print(f"{'case_id':<50} {'event_date':<12} {'registration':<10} {'site_slug':<30} {'narr_len':>8}")
    print("-" * 120)
    for row in con.execute(
        "SELECT case_id, event_date, registration, site_slug, length(narrative_text) as ll "
        "FROM sibpk_accidents ORDER BY event_date"
    ):
        print(f"  {row['case_id']:<48} {str(row['event_date'] or ''):<12} "
              f"{str(row['registration'] or ''):<10} {str(row['site_slug'] or ''):<30} {row['ll']:>8}")

    # OCR-TODO list
    ocr_rows = con.execute(
        "SELECT case_id, source_url FROM sibpk_reports WHERE status='ocr_todo'"
    ).fetchall()
    if ocr_rows:
        print(f"\n=== OCR-TODO ({len(ocr_rows)}) ===")
        for r in ocr_rows:
            print(f"  {r['case_id']}: {r['source_url']}")

    not_archived = con.execute(
        "SELECT case_id FROM sibpk_reports WHERE status='not_archived'"
    ).fetchall()
    if not_archived:
        print(f"\n=== NOT ARCHIVED ({len(not_archived)}) ===")
        for r in not_archived:
            print(f"  {r['case_id']}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    con = db_connect()

    if stage in ("discover", "all"):
        stage_discover(con)
    if stage in ("fetch", "all"):
        stage_fetch(con)
    if stage in ("parse", "all"):
        stage_parse(con)
    if stage in ("build", "all"):
        stage_build(con)
    if stage == "recheck":
        stage_recheck(con)
    if stage in ("stats",):
        stage_stats(con)

    if stage in ("all", "build"):
        stage_stats(con)

    con.close()

if __name__ == "__main__":
    main()
