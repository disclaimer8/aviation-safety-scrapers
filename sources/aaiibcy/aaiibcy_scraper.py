#!/usr/bin/env python3
"""aaiibcy — Cyprus AAIIB (Aircraft Accident and Incident Investigation Board)
aviation-accident ingest.

Source: www.aaiib.gov.cy — Adobe Domino/NSF site. Contrary to recon expectations,
the site DOES serve full HTML to curl (SpryAccordion is only cosmetic navigation).
The report listing is reachable via a direct fetch tree:
  page13_en -> year batch pages -> per-report sub-doc pages -> PDF links

Enumeration strategy:
  1. Fetch page13_en/page13_en?OpenDocument for year-batch NSF links (2015-2025)
  2. Optionally fetch page16_en/page16_en?OpenDocument for older batches
  3. Each year-batch page either has direct PDF hrefs OR sub-doc NSF links
  4. Sub-doc pages have the per-report PDF href
  5. Fallback: CDX wildcard scan on www.aaiib.gov.cy/* for any missed PDFs

PDF URL pattern:
  https://www.aaiib.gov.cy/mcw/DCA/AAIIB/aaiib.nsf/All/<DOCID>/$file/<FILENAME>.pdf
  (relative links in HTML use ../ or the DOCID directly)

case_id: 'aaiibcy-<ref>' where ref is parsed from report filename or title
  e.g. aaiibcy-1-22 (Final Report REF 1/22)
       aaiibcy-3-18 (accident 3/18)
       aaiibcy-da42-2014 (fallback slug from aircraft+year)

event_date: ACCIDENT date from PDF text (NOT publication date).
  Patterns: 'occurred on DD MONTH YYYY', 'on DDMM YYYY', ISO dates in header.

OCR: many PDFs are scanned (Producer: Scanner System). Use OCR_REMOTE=<ocr-host>
  (run as a1 on the remote) for those with pdftotext < FLOOR chars.

Narrative floor: 300 chars for final count.

Stages: discover | fetch | parse | build | all
"""

import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, uuid
import urllib.request

BASE = "https://www.aaiib.gov.cy"
NSF_PREFIX = "/mcw/DCA/AAIIB/aaiib.nsf"
FINAL_REPORTS_PAGE = BASE + NSF_PREFIX + "/page13_en/page13_en?OpenDocument"
OLDER_REPORTS_PAGE = BASE + NSF_PREFIX + "/page16_en/page16_en?OpenDocument"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 1.5       # inter-request delay
FLOOR = 80        # min chars to consider text usable
MIN_NARRATIVE = 300
HOME = os.path.expanduser("~/aaiibcy-ingest")
DB = os.path.join(HOME, "aaiibcy.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

OCR_REMOTE = os.environ.get("OCR_REMOTE", "")
OCR_LANG = "eng"

# ---- SCHEMA -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaiibcy_reports (
  case_id        TEXT PRIMARY KEY,
  nsf_doc_id     TEXT,           -- NSF document ID (hex) hosting this report
  pdf_url        TEXT,           -- canonical live URL
  pdf_path       TEXT,
  title          TEXT,           -- report title from page text
  ref_number     TEXT,           -- e.g. "1/22", "3-18"
  report_year    INT,
  report_type    TEXT DEFAULT 'Final report',
  registration   TEXT,
  event_date     TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  aircraft       TEXT,
  operator       TEXT,
  source_tier    TEXT,           -- 'pdf' | 'ocr' | 'scanned'
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS aaiibcy_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'CY',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'en',
  fatalities_total INT,
  phase          TEXT,
  category       TEXT,
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_aaiibcy_status ON aaiibcy_reports(status);
"""

def now():
    return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def _get(url, timeout=15):
    """Simple HTTP GET, returns bytes or raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()

def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote host. Returns '' on any failure."""
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    txt_remote = remote + ".txt"
    try:
        subprocess.run(["scp", "-q", pdf_path, f"{host}:{remote}"],
                       timeout=120, check=True)
        cmd = (f"nice -n 10 ionice -c3 ocrmypdf --rotate-pages --deskew "
               f"--language {lang} --output-type pdf "
               f"{remote} {remote}.ocr.pdf 2>/dev/null && "
               f"pdftotext -q {remote}.ocr.pdf {txt_remote} && "
               f"cat {txt_remote}; "
               f"rm -f {remote} {remote}.ocr.pdf {txt_remote}")
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, cmd],
                           capture_output=True, timeout=600)
        if r.returncode == 0:
            return r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"[aaiibcy ocr] remote OCR failed: {e}", file=sys.stderr)
    return ""

def slugify(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p]))
    return s.strip("-").lower()[:80] or None

# ---- PDF URL resolution -------------------------------------------------------

def resolve_pdf_url(base_doc_url, raw_href):
    """Convert a relative PDF href found in a sub-doc page to an absolute URL."""
    # raw_href examples:
    #   "4CAA14F1A8552D87C2258C120045690D/$file/FINAL REPORT REF 1-22.pdf"
    #   "../all/BCC063BC4D440324C225824B00356A30/$file/AIRCRAFT TYPE...pdf?openelement"
    #   "2178A7939A5022D1C225873E002F3E4B/$file/FINAL REPORT 1 2019.pdf"

    raw_href = raw_href.strip()

    # Already absolute
    if raw_href.startswith("http"):
        # Normalize ?openelement suffix (not needed for download)
        return re.sub(r'\?openelement$', '', raw_href, flags=re.I)

    # Strip ?openelement
    raw_href = re.sub(r'\?openelement$', '', raw_href, flags=re.I)

    # For relative URLs, we need to be careful about encoding.
    # The hrefs in NSF HTML may already be partially URL-encoded (e.g. %20 for spaces)
    # or unencoded. We want to produce a valid final URL.

    if raw_href.startswith("../all/") or raw_href.startswith("../All/"):
        # ../all/<DOCID>/$file/<name>.pdf -> absolute
        # The part after ../all/ may already be encoded; re-encode only the filename portion
        rest = raw_href[7:]  # strip ../all/
        # Split at /$file/ boundary
        if "/$file/" in rest:
            docid_fn = rest.split("/$file/", 1)
            docid = docid_fn[0]
            fn = docid_fn[1]
            # fn may be unencoded or encoded; normalize by decode-then-encode
            fn_decoded = urllib.parse.unquote(fn)
            fn_encoded = urllib.parse.quote(fn_decoded, safe='')
            return f"{BASE}{NSF_PREFIX}/all/{docid}/$file/{fn_encoded}"
        return f"{BASE}{NSF_PREFIX}/all/{rest}"
    elif re.match(r'^[A-F0-9]{32,}/', raw_href, re.I):
        # DOCID/$file/name.pdf — resolve against NSF All path
        if "/$file/" in raw_href:
            docid_fn = raw_href.split("/$file/", 1)
            docid = docid_fn[0]
            fn_decoded = urllib.parse.unquote(docid_fn[1])
            fn_encoded = urllib.parse.quote(fn_decoded, safe='')
            return f"{BASE}{NSF_PREFIX}/All/{docid}/$file/{fn_encoded}"
        return f"{BASE}{NSF_PREFIX}/All/{raw_href}"
    else:
        # Resolve against base_doc_url
        result = urllib.parse.urljoin(base_doc_url, raw_href)
        # Fix any double-encoding that might result
        return result

# ---- case_id parsing ---------------------------------------------------------

# REF patterns in filenames and titles:
# "FINAL REPORT REF 1-22.pdf"  -> 1-22  -> aaiibcy-1-22
# "FINAL REPORT REF 2 22.pdf"  -> 2-22
# "FINAL REPORT REF 9 22.pdf"  -> 9-22
# "FINAL RERPORT REF 5 22.pdf" -> 5-22
# "FINAL REPORT REF 3 22.pdf"  -> 3-22
# "FINAL REPORT REF 4 22.pdf"  -> 4-22
# "7. FINAL REPORT REF 9 22.pdf" -> 9-22
# "FINAL REPORT  REF 1-21.pdf" -> 1-21
# "FINAL REPORT FEF 1 23.pdf"  -> 1-23 (typo FEF)
# "FINAL REPORT REF 4 23.pdf"  -> 4-23
# "FINAL REPORT REF 1 24.pdf"  -> 1-24
# "FINAL REPORT 1 2019.pdf"    -> 1-19
# "FINAL REPORT 4 20.pdf"      -> 4-20
# "FINAL REPORT 1 2020 .pdf"   -> 1-20
# "FINAL REPORT 3 2020.pdf"    -> 3-20
# "FINAL REPORT OF ACCIDENT WHICH OCCURRED ON 10.05.18  (3 18).pdf" -> 3-18
# "FINAL AUILA AT 01 28 4 18.pdf"  -> fallback
# "BRM AERO BRISTEL FINAL REPORT.pdf" -> fallback
# "AIRCRAFT TYPE BRISTEL LSA 5B-HBI INCIDENT ON 9 AUGUST 2016.pdf" -> fallback
# "Final Report AAIIB 16.15.01.7-23 V2.0_Website.pdf" -> 16.15.01.7-23 (use file ref)
# "PARAMOTOR Accident due Wing Collapse_V1.1_website.pdf" -> fallback
# "CCF_000592.pdf" -> fallback (2025 report)
# "FINAL REPORT ON AQUILA AT01 AIRCRAFT..." -> year from context
# "FINAL REPORT OF PARAMOTOR GLIDER ACCIDENT IN AKAMAS PENINSULA ON 5 AUGUST 2014.pdf"
# "FINAL REPORT OF DA42 AIRCRAFT 47NM S.E. OF LCA ON 22.10.2014.pdf"

_REF_PAT1 = re.compile(r'(?:REF|FEF)[.\s]+(\d+)[\s\-/]+(\d{2,4})\.pdf', re.I)
_REF_PAT2 = re.compile(r'\((\d+)\s+(\d{2})\)\.pdf', re.I)           # (3 18).pdf
_REF_PAT3 = re.compile(r'REPORT\s+(\d+)\s+(20\d{2})', re.I)          # REPORT 1 2019
_REF_PAT4 = re.compile(r'REPORT\s+(\d+)\s+(\d{2})\s*\.pdf', re.I)    # REPORT 4 20.pdf
_REF_PAT5 = re.compile(r'AAIIB\s+([\d.]+\d-\d{2})', re.I)            # AAIIB 16.15.01.7-23
_REF_PAT6 = re.compile(r'REF[\s.]*(\d+)[\s/\-]+(\d{2,4})', re.I)     # REF 1-22 or REF 1/22

def parse_ref(filename, title="", year_hint=None):
    """
    Extract a normalized ref number from filename/title.
    Returns (case_id_suffix, ref_number) where ref_number is like '1/22'.
    """
    fn = urllib.parse.unquote(os.path.basename(filename))
    fn_no_ext = re.sub(r'\.pdf.*$', '', fn, flags=re.I).strip()

    # Pattern 1: REF N-YY or REF N YY in filename (using URL-decoded fn)
    for pat in [_REF_PAT1, _REF_PAT6]:
        m = pat.search(fn)
        if m:
            num, yr = m.group(1), m.group(2)
            yr2 = yr[-2:]  # last 2 digits
            return f"{num}-{yr2}", f"{num}/{yr2}"

    # Pattern 2: (3 18) in filename
    m = _REF_PAT2.search(fn)
    if m:
        num, yr = m.group(1), m.group(2)
        return f"{num}-{yr}", f"{num}/{yr}"

    # Pattern 3: REPORT N 2019 in filename
    m = _REF_PAT3.search(fn)
    if m:
        num, yr = m.group(1), m.group(2)[-2:]
        return f"{num}-{yr}", f"{num}/{yr}"

    # Pattern 4: REPORT N NN .pdf
    m = _REF_PAT4.search(fn)
    if m:
        num, yr = m.group(1), m.group(2)
        return f"{num}-{yr}", f"{num}/{yr}"

    # Pattern 5: AAIIB file reference
    m = _REF_PAT5.search(fn)
    if m:
        ref = re.sub(r'[^\d.-]', '', m.group(1))
        return slugify(ref), ref

    # Try title
    if title:
        m = re.search(r'REF[.\s/]+(\d+)[/\s-]+(\d{2,4})', title, re.I)
        if m:
            num, yr = m.group(1), m.group(2)[-2:]
            return f"{num}-{yr}", f"{num}/{yr}"

    # Fallback: extract registration or aircraft clue from filename (URL-decoded)
    # DA42 2014
    m = re.search(r'DA42.*?(\d{4})', fn, re.I)
    if m:
        return f"da42-{m.group(1)[-2:]}", f"DA42/{m.group(1)}"

    # PARAMOTOR ... date
    if re.search(r'PARAMOTOR|PARAGLIDER', fn, re.I):
        yr = re.search(r'(20\d{2})', fn)
        yr_str = yr.group(1)[-2:] if yr else (str(year_hint)[-2:] if year_hint else "xx")
        return f"paramotor-{yr_str}", f"paramotor/{yr_str}"

    # BRISTEL with year context
    if re.search(r'BRISTEL', fn, re.I):
        yr = re.search(r'(20\d{2})', fn)
        yr_str = yr.group(1)[-2:] if yr else (str(year_hint)[-2:] if year_hint else "xx")
        return f"bristel-{yr_str}", f"bristel/{yr_str}"

    # AQUILA AT01 with year context
    m_aquila = re.search(r'AQUILA.*?(?:(\d{1,2})\s+(\w+)\s+(20\d{2})|(20\d{2}))', fn, re.I)
    if m_aquila:
        yr_str = (m_aquila.group(3) or m_aquila.group(4) or "")[-2:] or (str(year_hint)[-2:] if year_hint else "xx")
        # Disambiguate multiple Aquila reports by month/day
        d_m = re.search(r'(\d{1,2})\s+(\w+)\s+(20\d{2})', fn, re.I)
        if d_m:
            mon = d_m.group(2)[:3].lower()
            return f"aquila-{mon}-{yr_str}", f"aquila/{mon}-{yr_str}"
        return f"aquila-{yr_str}", f"aquila/{yr_str}"

    # AVIATOR
    if re.search(r'AVIATOR', fn, re.I):
        yr_str = str(year_hint)[-2:] if year_hint else "xx"
        return f"aviator-{yr_str}", f"aviator/{yr_str}"

    # NEMAX
    if re.search(r'NEMAX', fn, re.I):
        d_m = re.search(r'(\d{1,2})\s+(\w{3,})\s+(20\d{2})', fn, re.I)
        if d_m:
            mon = d_m.group(2)[:3].lower()
            yr_str = d_m.group(3)[-2:]
            return f"nemax-{mon}-{yr_str}", f"nemax/{mon}-{yr_str}"
        yr_str = str(year_hint)[-2:] if year_hint else "xx"
        return f"nemax-{yr_str}", f"nemax/{yr_str}"

    # AUILA (typo in filename) = AQUILA
    if re.search(r'AUILA', fn, re.I):
        yr_str = str(year_hint)[-2:] if year_hint else "xx"
        return f"aquila-apr-{yr_str}", f"aquila/apr-{yr_str}"

    # CCF_ (2025)
    if fn.startswith("CCF_"):
        return "ccf-25", "CCF/25"

    # Generic fallback using first meaningful word groups + year
    yr_str = str(year_hint)[-2:] if year_hint else "xx"
    words = re.findall(r'[A-Za-z]{4,}', fn_no_ext)
    slug_base = "-".join(words[:3]).lower()[:30] if words else "report"
    return f"{slug_base}-{yr_str}", fn_no_ext[:30]


# ---- Date parsing from PDF text ----------------------------------------------

_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

def _parse_named_date(d_str, mo_str, y_str):
    """Parse day/month-name/year strings. Returns 'YYYY-MM-DD' or None."""
    mo = _MONTHS.get(mo_str.lower())
    if not mo:
        return None
    try:
        d = int(re.sub(r'\D', '', d_str))
        y = int(y_str)
        if 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y}-{mo:02d}-{d:02d}"
    except ValueError:
        pass
    return None

_MONTH_NAME = r'(January|February|March|April|May|June|July|August|September|October|November|December)'

# High-priority: accident date preceded by "occurred on", "which occurred", "on the Nth"
_DATE_PRIORITY = [
    # "occurred on 22 October 2014" / "occurred on the 22nd of October 2014"
    re.compile(r'occurred\s+on\s+(?:the\s+)?(\d{1,2})(?:ST|ND|RD|TH)?\s+(?:of\s+)?' + _MONTH_NAME + r'\s+(20\d{2})', re.I),
    # "WHICH OCCURRED ON 1ST FEBRUARY 2017"
    re.compile(r'OCCURRED\s+ON\s+(\d{1,2})(?:ST|ND|RD|TH)?\s+' + _MONTH_NAME + r'\s+(20\d{2})', re.I),
    # "ON THE 22ND OCTOBER 2014" in subject line
    re.compile(r'ON\s+THE\s+(\d{1,2})(?:ST|ND|RD|TH)?\s+(?:OF\s+)?' + _MONTH_NAME + r'\s+(20\d{2})', re.I),
    # "ON 5 AUGUST 2014" standalone
    re.compile(r'\bON\s+(\d{1,2})\s+' + _MONTH_NAME + r'\s+(20\d{2})', re.I),
    # "9 AUGUST 2016" standalone (title form)
    re.compile(r'\b(\d{1,2})\s+' + _MONTH_NAME + r'\s+(20\d{2})', re.I),
    # "22.10.2014" or "10/05/2018"
    re.compile(r'\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b'),
    # "(10.05.18)" format
    re.compile(r'\((\d{1,2})\.(\d{2})\.(\d{2})\)'),
    # ISO "2014-10-22"
    re.compile(r'\b(20\d{2})-(\d{2})-(\d{2})\b'),
]

def _safe_date(y, m, d):
    """Return YYYY-MM-DD if valid and within reasonable range, else None."""
    try:
        yi, mi, di = int(y), int(m), int(d)
        if 2000 <= yi <= 2030 and 1 <= mi <= 12 and 1 <= di <= 31:
            return f"{yi}-{mi:02d}-{di:02d}"
    except (ValueError, TypeError):
        pass
    return None

def _scan_for_dates(text):
    """Generator: yield (priority, YYYY-MM-DD) for all date patterns found.
    Priority 0 = highest (explicit 'occurred on'), 4 = lowest (standalone numeric).
    """
    # Mask out regulation references
    clean = re.sub(r'\b(?:No\s+)?9\d{2}/20\d{2}\b', 'REGREF', text)
    clean = re.sub(r'\b(?:No\s+)?\d+\(I\)/20\d{2}\b', 'ACTREF', clean)

    MN = r'(January|February|March|April|May|June|July|August|September|October|November|December)'

    # Priority 0: "occurred on DD(th) Month YYYY" / "ON THE DDth of Month YYYY"
    for m in re.finditer(
        r'(?:occurred\s+on\s+(?:the\s+)?|on\s+the\s+)(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?' + MN + r'\s+(20\d{2})',
        clean, re.I
    ):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (0, r)

    # Priority 0: "WHICH OCCURRED ON DDth Month YYYY"
    for m in re.finditer(
        r'OCCURRED\s+ON\s+(\d{1,2})(?:ST|ND|RD|TH)?\s+' + MN + r'\s+(20\d{2})',
        clean, re.I
    ):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (0, r)

    # Priority 1: "ON DD Month YYYY" (near SUBJECT/accident description)
    for m in re.finditer(r'\bON\s+(\d{1,2})\s+' + MN + r'\s+(20\d{2})', clean, re.I):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (1, r)

    # Priority 1: "on the DDth, Month, YYYY" (newer format, with or without "the")
    for m in re.finditer(r'on\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?[",\s]+\s*' + MN + r'[,\s]+(20\d{2})', clean, re.I):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (1, r)

    # Priority 1: "on DDth Month," (OCR variant with malformed ordinal)
    for m in re.finditer(r'on\s+(\d{1,2})(?:st|nd|rd|th|["“”]+)\s+' + MN + r',?\s+(20\d{2})', clean, re.I):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (1, r)

    # Priority 2: "DD Month YYYY" standalone (not after a verb)
    for m in re.finditer(r'\b(\d{1,2})\s+' + MN + r'\s+(20\d{2})', clean, re.I):
        r = _parse_named_date(m.group(1), m.group(2), m.group(3))
        if r: yield (2, r)

    # Priority 2: ACCIDENT ON DD/MM/YY or DD/MM/YYYY in title context
    for m in re.finditer(r'ACCIDENT\s+ON\s+(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})', clean, re.I):
        d, mo, y = m.group(1), m.group(2), m.group(3)
        yi = 2000 + int(y) if len(y) == 2 else int(y)
        r = _safe_date(yi, mo, d)
        if r: yield (2, r)

    # Priority 3: "DD.MM.YYYY" numeric dates
    for m in re.finditer(r'\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b', clean):
        r = _safe_date(m.group(3), m.group(2), m.group(1))
        if r: yield (3, r)

    # Priority 4: ISO dates
    for m in re.finditer(r'\b(20\d{2})-(\d{2})-(\d{2})\b', clean):
        r = _safe_date(m.group(1), m.group(2), m.group(3))
        if r: yield (4, r)


def parse_date(text):
    """Extract ACCIDENT date from PDF text. Returns 'YYYY-MM-DD' or None."""
    if not text:
        return None

    # Search first 4000 chars (header section)
    excerpt = text[:4000]
    best_pri = 999
    best_date = None

    for pri, dt in _scan_for_dates(excerpt):
        if pri < best_pri:
            best_pri = pri
            best_date = dt
            if pri == 0:
                break  # Can't do better than priority 0

    return best_date


# ---- Registration parsing ---------------------------------------------------

_REG_RE = re.compile(r'\b(5B-[A-Z]{2,3})\b')

def parse_registration(text):
    m = _REG_RE.search(text or "")
    return m.group(1) if m else None


# ---- Aircraft type parsing --------------------------------------------------

_AIRCRAFT_PATS = [
    re.compile(r'AIRCRAFT\s+TYPE\s+([A-Z][A-Za-z0-9\s/-]{3,30}?)(?:\s+REGISTRATION|\s+REG|\s+5B-|\n)', re.I),
    re.compile(r'TYPE[:\s]+([A-Z][A-Za-z0-9\s/-]{3,25}?)(?:\s+REGISTRATION|\s+REG|\s+5B-|\n)', re.I),
    re.compile(r'(AQUILA\s*AT\s*0?1|DA42|PARAMOTOR|PARAGLIDER|BRISTEL\s*LSA|TECNAM\s*P\d+|BOEING\s+\d+|CL\s*600|CESSNA\s*[A-Z]?\d+|CIRRUS\s*SR\d+|PIPER|CESSNA)', re.I),
]

def parse_aircraft(text):
    for pat in _AIRCRAFT_PATS:
        m = pat.search(text or "")
        if m:
            return m.group(1).strip()[:50]
    return None


# ---- Location parsing -------------------------------------------------------

_LOC_PATS = [
    re.compile(r'(?:at|near|over)\s+((?:Larnaca|Paphos|Nicosia|Limassol|LCPH|LCA|LCLK|Akamas|Athienou|Ammochostos)[^,\n]{0,40})', re.I),
    re.compile(r'LOCATION[:\s]+([^\n]{5,60})', re.I),
    re.compile(r'AT\s+(LCA(?:RNACA)?|PAPHOS|LCLK|LCPH)\s*(?:AIRPORT|AERODROME|INTERNATIONAL)?', re.I),
]

def parse_location(text):
    for pat in _LOC_PATS:
        m = pat.search(text or "")
        if m:
            loc = m.group(1).strip()
            if len(loc) > 4:
                return loc[:80]
    return "Cyprus"


# ---- Narrative extraction ---------------------------------------------------

def extract_narrative(text):
    """
    Extract the main narrative body from the PDF text.
    Typical structure: Title block, OBJECTIVE, then numbered sections with
    1. FACTUAL INFORMATION, 2. ANALYSIS, 3. CONCLUSIONS, 4. SAFETY RECOMMENDATIONS
    We want everything from section 1 to the end of Analysis/Conclusions.
    """
    if not text or len(text) < FLOOR:
        return text or ""

    # Try to find section 1 (Factual Information)
    m = re.search(r'(?:1\.|SECTION\s+1|1\s+FACTUAL\s+INFORMATION)', text, re.I)
    if m:
        return text[m.start():][:8000]

    # Fallback: skip header (first 200 chars) and return rest
    return text[200:][:8000]


def extract_probable_cause(text):
    """Extract probable cause / findings section."""
    m = re.search(r'(?:CAUSE|PROBABLE\s+CAUSE|FINDINGS|CONCLUSIONS?)[:\s]*\n(.{50,2000}?)(?:\n\n|\Z)',
                  text or "", re.I | re.DOTALL)
    if m:
        return m.group(1).strip()[:2000]
    return None


# ---- HTML fetch helpers -----------------------------------------------------

def fetch_html(url):
    """Fetch a page, return decoded text. Retries once on timeout."""
    for attempt in range(2):
        try:
            data = _get(url, timeout=15)
            return data.decode("utf-8", "replace")
        except Exception as e:
            if attempt == 0:
                time.sleep(3)
                continue
            print(f"[aaiibcy] fetch failed {url}: {e}", file=sys.stderr)
            return ""

_NSF_DOC_RE = re.compile(
    r'href="(/mcw/DCA/AAIIB/aaiib\.nsf/[Aa]ll/([A-F0-9]{32,})\?OpenDocument)"',
    re.I
)
_PDF_HREF_RE = re.compile(r'href="([^"]*\.pdf[^"]*)"', re.I)
_TITLE_RE = re.compile(
    r'href="[^"]+OpenDocument"[^>]*>([^<]{10,150})</a>',
    re.I
)
_SKIP_LABELS = {'HOME', 'ABOUT US', 'PUBLICATIONS', 'CONTACT US', 'Homepage',
                'FAQ', 'Home Page', 'Disclaimer', 'Webmaster', 'REPORTING',
                'Contact', 'Final Reports', 'index_en', 'index_gr'}


def _is_skip_label(txt):
    t = txt.strip()
    for s in _SKIP_LABELS:
        if s.lower() in t.lower():
            return True
    return False


def get_doc_links(html, base_url=None):
    """Return list of (path, docid) tuples from NSF All/ document links."""
    results = []
    for m in _NSF_DOC_RE.finditer(html):
        path = m.group(1)
        docid = m.group(2).upper()
        results.append((path, docid))
    # Deduplicate by docid
    seen = set()
    out = []
    for path, docid in results:
        if docid not in seen:
            seen.add(docid)
            out.append((path, docid))
    return out


def get_pdf_hrefs(html):
    """Return list of unique PDF href strings from an HTML page."""
    seen = set()
    result = []
    for m in _PDF_HREF_RE.finditer(html):
        href = m.group(1)
        if href not in seen:
            seen.add(href)
            result.append(href)
    return result


def get_page_labels(html):
    """Return report title labels from NSF doc links on a page."""
    labels = []
    for m in _TITLE_RE.finditer(html):
        txt = m.group(1).strip()
        if not _is_skip_label(txt) and len(txt) > 8:
            labels.append(txt)
    return labels


# ---- CDX fallback -----------------------------------------------------------

def normalize_pdf_url(url):
    """Normalize PDF URL: https, proper case /All/, remove ?openelement, fix double-encoding."""
    url = re.sub(r'\?openelement$', '', url, flags=re.I)
    url = re.sub(r'\?$', '', url)
    url = url.replace("http://", "https://").replace(":80/", "/")
    # Fix double-encoded URLs: %2520 -> %20 (happens when resolve_pdf_url gets
    # an href that was already partially percent-encoded)
    url = url.replace("%2520", "%20")
    return url

def cdx_scan(c):
    """CDX wildcard scan for any PDF on aaiib.gov.cy not already discovered."""
    url = (f"{CDX_BASE}?url=www.aaiib.gov.cy/*"
           f"&output=json&fl=original,timestamp&filter=statuscode:200"
           f"&filter=original:.*\\.pdf&collapse=original&limit=200")
    try:
        data = json.loads(_get(url, timeout=30))
    except Exception as e:
        print(f"[aaiibcy cdx] error: {e}", file=sys.stderr)
        return 0

    added = 0
    # Build set of known docids to avoid duplicates
    known_docids = set()
    for row in c.execute("SELECT nsf_doc_id FROM aaiibcy_reports WHERE nsf_doc_id IS NOT NULL").fetchall():
        known_docids.add(row[0].upper())

    _CDX_SKIP = re.compile(
        r'occurrence.report|safety.review|safety.reviews|occurrence.form',
        re.I
    )

    for row in data[1:]:
        orig = row[0]
        if _CDX_SKIP.search(orig):
            continue
        if "/$file/" not in orig:
            continue

        clean = normalize_pdf_url(orig)

        # Extract docid
        m = re.search(r'/[Aa]ll/([A-F0-9]{32,})/', clean, re.I)
        if not m:
            continue
        docid = m.group(1).upper()

        # Skip if we already have this docid
        if docid in known_docids:
            continue

        fn = urllib.parse.unquote(clean.split("/$file/")[-1])
        suffix, ref = parse_ref(fn, year_hint=None)
        cid = f"aaiibcy-{suffix}"

        # Check for case_id collision
        existing = c.execute("SELECT case_id FROM aaiibcy_reports WHERE case_id=?", (cid,)).fetchone()
        if existing:
            cid = f"aaiibcy-cdx-{docid[:8].lower()}"

        c.execute(
            "INSERT OR IGNORE INTO aaiibcy_reports "
            "(case_id, nsf_doc_id, pdf_url, ref_number, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, docid, clean, ref, 'new', now(), now())
        )
        c.commit()
        known_docids.add(docid)
        added += 1

    return added


# ---- Discover stage ---------------------------------------------------------

def discover(c):
    """
    Crawl the report listing pages and populate aaiibcy_reports with PDF URLs.
    Returns count of new rows inserted.
    """
    inserted = 0

    known_docids = set()

    def insert_pdf(pdf_url, nsf_doc_id, title, year_hint):
        pdf_url = normalize_pdf_url(pdf_url)
        docid_upper = nsf_doc_id.upper()

        # Deduplicate by NSF doc ID (same PDF can appear under different URLs)
        if docid_upper in known_docids:
            return False
        known_docids.add(docid_upper)

        fn = urllib.parse.unquote(pdf_url.split("/$file/")[-1]) if "/$file/" in pdf_url else pdf_url
        fn = fn.split("?")[0]
        suffix, ref = parse_ref(fn, title=title, year_hint=year_hint)
        cid = f"aaiibcy-{suffix}"

        # If case_id collision, append docid suffix
        existing_cid = c.execute(
            "SELECT case_id FROM aaiibcy_reports WHERE case_id=?", (cid,)
        ).fetchone()
        if existing_cid:
            cid = f"aaiibcy-{suffix}-{nsf_doc_id[:6].lower()}"

        c.execute(
            "INSERT OR IGNORE INTO aaiibcy_reports "
            "(case_id, nsf_doc_id, pdf_url, title, ref_number, report_year, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, docid_upper, pdf_url, title, ref, year_hint, 'new', now(), now())
        )
        c.commit()
        return c.execute("SELECT changes()").fetchone()[0] > 0

    def crawl_listing_page(page_url):
        """Crawl a listing page (page13_en or page16_en) and all its children."""
        nonlocal inserted
        print(f"[discover] fetching listing: {page_url}")
        html = fetch_html(page_url)
        if not html:
            return

        # Get year-batch sub-doc links, with year extracted from surrounding text
        year_doc_links = get_doc_links(html)
        # Extract year hints: find the text around each doc link
        year_hints_map = {}
        for path, docid in year_doc_links:
            # Search for year numbers in the vicinity of this docid in the HTML
            idx = html.upper().find(docid)
            if idx >= 0:
                context = html[max(0, idx-100):idx+200]
                ym = re.findall(r'(20\d{2})', context)
                if ym:
                    year_hints_map[docid] = int(ym[0])
        print(f"[discover] found {len(year_doc_links)} year-batch links, year_hints: {year_hints_map}")

        for i, (path, docid) in enumerate(year_doc_links):
            year_hint = year_hints_map.get(docid)

            year_url = BASE + path
            time.sleep(DELAY)
            print(f"[discover] fetching year page: {year_url} (year={year_hint})")
            year_html = fetch_html(year_url)
            if not year_html:
                continue

            # Check for direct PDF hrefs on year page
            direct_pdfs = get_pdf_hrefs(year_html)
            if direct_pdfs:
                print(f"[discover]   {len(direct_pdfs)} direct PDFs")
                for href in direct_pdfs:
                    abs_url = resolve_pdf_url(year_url, href)
                    # Extract doc ID from url
                    m = re.search(r'/[Aa]ll/([A-F0-9]{32,})/', abs_url, re.I)
                    doc_id = m.group(1).upper() if m else docid
                    fn = urllib.parse.unquote(abs_url.split("/$file/")[-1]) if "/$file/" in abs_url else abs_url
                    if insert_pdf(abs_url, doc_id, "", year_hint):
                        inserted += 1
                        print(f"[discover]     + {fn[:60]}")
                time.sleep(DELAY)
                continue

            # Check for per-report sub-doc links
            sub_doc_links = get_doc_links(year_html)
            sub_labels = get_page_labels(year_html)
            if sub_doc_links:
                print(f"[discover]   {len(sub_doc_links)} per-report sub-docs, labels: {sub_labels[:3]}")
                for j, (sub_path, sub_docid) in enumerate(sub_doc_links):
                    sub_label = sub_labels[j] if j < len(sub_labels) else ""
                    sub_url = BASE + sub_path
                    time.sleep(DELAY)
                    sub_html = fetch_html(sub_url)
                    if not sub_html:
                        continue
                    sub_pdfs = get_pdf_hrefs(sub_html)
                    if sub_pdfs:
                        for href in sub_pdfs[:1]:  # take first PDF per sub-doc
                            abs_url = resolve_pdf_url(sub_url, href)
                            if insert_pdf(abs_url, sub_docid, sub_label, year_hint):
                                inserted += 1
                                fn = urllib.parse.unquote(abs_url.split("/$file/")[-1]) if "/$file/" in abs_url else abs_url
                                print(f"[discover]     + {fn[:60]} ({sub_label[:40]})")
                    else:
                        # Sub-doc may itself have sub-sub-docs (unlikely but check)
                        subsub = get_doc_links(sub_html)
                        for sub2_path, sub2_docid in subsub[:3]:
                            sub2_url = BASE + sub2_path
                            time.sleep(DELAY)
                            sub2_html = fetch_html(sub2_url)
                            if not sub2_html:
                                continue
                            for href in get_pdf_hrefs(sub2_html)[:1]:
                                abs_url = resolve_pdf_url(sub2_url, href)
                                if insert_pdf(abs_url, sub2_docid, sub_label, year_hint):
                                    inserted += 1
                                    fn = urllib.parse.unquote(abs_url.split("/$file/")[-1])
                                    print(f"[discover]     + (sub2) {fn[:60]}")
                    time.sleep(DELAY)
        return

    # Main listing: page13_en (2015-2025)
    crawl_listing_page(FINAL_REPORTS_PAGE)
    time.sleep(DELAY)

    # Older listing: page16_en (pre-2015 era)
    crawl_listing_page(OLDER_REPORTS_PAGE)
    time.sleep(DELAY)

    # CDX fallback
    print("[discover] CDX fallback scan...")
    cdx_new = cdx_scan(c)
    print(f"[discover] CDX added {cdx_new} new rows")
    inserted += cdx_new

    total = c.execute("SELECT COUNT(*) FROM aaiibcy_reports").fetchone()[0]
    print(f"[discover] done: {inserted} new rows, {total} total in DB")
    return inserted


# ---- Fetch stage ------------------------------------------------------------

def fetch_pdfs(c):
    """Download all PDFs with status='new'."""
    rows = c.execute(
        "SELECT case_id, pdf_url FROM aaiibcy_reports WHERE status='new' AND pdf_url IS NOT NULL"
    ).fetchall()
    print(f"[fetch] {len(rows)} PDFs to download")
    done = 0
    fails = 0

    for row in rows:
        cid = row["case_id"]
        url = row["pdf_url"]
        # Build safe filename
        fn_raw = urllib.parse.unquote(url.split("/$file/")[-1]).split("?")[0] if "/$file/" in url else cid + ".pdf"
        fn_safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", fn_raw)[:120]
        if not fn_safe.lower().endswith(".pdf"):
            fn_safe += ".pdf"
        dest = os.path.join(PDFDIR, fn_safe)

        # Avoid re-download if file exists and is non-empty
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            c.execute("UPDATE aaiibcy_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), cid))
            c.commit()
            done += 1
            continue

        try:
            print(f"[fetch] {cid}: {fn_raw[:60]}")
            data = _get(url, timeout=30)
            with open(dest, "wb") as fh:
                fh.write(data)
            c.execute("UPDATE aaiibcy_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), cid))
            c.commit()
            done += 1
            fails = 0
        except Exception as e:
            print(f"[fetch] FAIL {cid}: {e}", file=sys.stderr)
            fails += 1
            c.execute("UPDATE aaiibcy_reports SET status='fetch_fail', skip_reason=?, updated_at=? WHERE case_id=?",
                      (str(e)[:200], now(), cid))
            c.commit()
            if fails >= 5:
                print("[fetch] too many consecutive failures, stopping", file=sys.stderr)
                break
        time.sleep(DELAY)

    print(f"[fetch] done: {done}/{len(rows)}")
    return done


# ---- Parse stage ------------------------------------------------------------

def parse(c):
    """Extract text from fetched PDFs, OCR scanned ones if OCR_REMOTE set."""
    rows = c.execute(
        "SELECT case_id, pdf_path, title, report_year FROM aaiibcy_reports WHERE status='fetched'"
    ).fetchall()
    print(f"[parse] {len(rows)} PDFs to parse")

    scanned = []
    done = 0
    for row in rows:
        cid = row["case_id"]
        path = row["pdf_path"]
        title = row["title"] or ""
        year = row["report_year"]

        txt = extract_text(path)
        if len(txt) >= FLOOR:
            tier = "pdf"
        else:
            scanned.append(row)
            tier = "scanned"
            txt = ""

        reg = parse_registration(txt)
        ev_date = parse_date(txt)
        aircraft = parse_aircraft(txt) or parse_aircraft(title)
        location = parse_location(txt)
        narrative = extract_narrative(txt)
        probable_cause = extract_probable_cause(txt)

        c.execute(
            "UPDATE aaiibcy_reports SET "
            "narrative_text=?, source_tier=?, registration=?, event_date=?, "
            "aircraft=?, location=?, probable_cause=?, status='parsed', updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, reg, ev_date, aircraft, location, probable_cause, now(), cid)
        )
        c.commit()
        done += 1

    print(f"[parse] {done} parsed, {len(scanned)} scanned (need OCR)")

    # OCR scanned PDFs
    if scanned and OCR_REMOTE:
        print(f"[parse] OCR via {OCR_REMOTE}...")
        for row in scanned:
            cid = row["case_id"]
            path = row["pdf_path"]
            title = row["title"] or ""
            if not path or not os.path.exists(path):
                continue
            print(f"[parse] OCR {cid}...")
            txt = _ocr_remote(path, OCR_LANG, OCR_REMOTE)
            if len(txt) >= FLOOR:
                tier = "ocr"
                reg = parse_registration(txt)
                ev_date = parse_date(txt)
                aircraft = parse_aircraft(txt) or parse_aircraft(title)
                location = parse_location(txt)
                narrative = extract_narrative(txt)
                probable_cause = extract_probable_cause(txt)
                c.execute(
                    "UPDATE aaiibcy_reports SET "
                    "narrative_text=?, source_tier=?, registration=?, event_date=?, "
                    "aircraft=?, location=?, probable_cause=?, status='parsed', updated_at=? "
                    "WHERE case_id=?",
                    (narrative, tier, reg, ev_date, aircraft, location, probable_cause, now(), cid)
                )
            else:
                c.execute("UPDATE aaiibcy_reports SET source_tier='scanned', status='parsed', updated_at=? WHERE case_id=?",
                          (now(), cid))
            c.commit()
            time.sleep(2)
    elif scanned:
        print(f"[parse] WARNING: {len(scanned)} scanned PDFs, set OCR_REMOTE to process them")

    return done


# ---- Build stage ------------------------------------------------------------

def build(c):
    """Populate aaiibcy_accidents from parsed reports."""
    rows = c.execute(
        "SELECT * FROM aaiibcy_reports WHERE status='parsed'"
    ).fetchall()
    print(f"[build] building {len(rows)} accidents")
    built = 0

    for row in rows:
        cid = row["case_id"]
        txt = row["narrative_text"] or ""
        title = row["title"] or ""

        # event_date: from parsed PDF or fall back to title date
        ev_date = row["event_date"]
        if not ev_date and title:
            # Try to parse date from title (e.g. "10/10/2021", "5th November 2023", "29/11/2020")
            # First try the named-date patterns in title
            td = parse_date(title)
            if td:
                ev_date = td
            else:
                # Try DD/MM/YYYY or DD/MM/YY in title
                m = re.search(r'(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})', title)
                if m:
                    d, mo, y = m.group(1), m.group(2), m.group(3)
                    yi = 2000 + int(y) if len(y) == 2 and int(y) < 50 else int(y)
                    ev_date = _safe_date(yi, mo, d)

        # Derive year_hint from case_id suffix if not available
        if not ev_date and row["report_year"]:
            yr = row["report_year"]
            # We only have year, not exact date
            ev_date = None  # leave NULL — don't fabricate

        aircraft = row["aircraft"] or parse_aircraft(title)
        registration = row["registration"] or parse_registration(title)
        location = row["location"] or "Cyprus"

        # Narrative: if too short, use title as stub
        narrative = txt.strip()
        if len(narrative) < 50 and title:
            narrative = title

        # site_slug
        slug_parts = [cid]
        if aircraft:
            slug_parts.append(slugify(aircraft))
        if ev_date:
            slug_parts.append(ev_date[:4])
        site_slug = "-".join(slug_parts)[:100]

        c.execute(
            "INSERT OR REPLACE INTO aaiibcy_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                ev_date,
                aircraft,
                registration,
                row["operator"],
                location,
                "CY",
                narrative,
                row["probable_cause"],
                row["pdf_url"],
                row["report_type"] or "Final report",
                site_slug,
                "en",
                now(),
            )
        )
        c.commit()
        built += 1

    # Stats
    total = c.execute("SELECT COUNT(*) FROM aaiibcy_accidents").fetchone()[0]
    with_narr = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_accidents WHERE length(narrative_text) >= ?", (MIN_NARRATIVE,)
    ).fetchone()[0]
    with_date = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_accidents WHERE event_date IS NOT NULL"
    ).fetchone()[0]
    dups = c.execute(
        "SELECT COUNT(*) FROM (SELECT case_id, COUNT(*) AS n FROM aaiibcy_accidents GROUP BY case_id HAVING n > 1)"
    ).fetchone()[0]

    print(f"[build] total={total} | ≥300chars={with_narr} | dated={with_date} | dups={dups}")
    print("[build] sample case_ids:")
    for r in c.execute("SELECT case_id, event_date, aircraft FROM aaiibcy_accidents ORDER BY event_date LIMIT 10").fetchall():
        print(f"  {r['case_id']} | {r['event_date']} | {r['aircraft']}")

    return built


# ---- Verify stage -----------------------------------------------------------

def verify(c):
    """Print verification report."""
    total = c.execute("SELECT COUNT(*) FROM aaiibcy_accidents").fetchone()[0]
    with_narr = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_accidents WHERE length(narrative_text) >= ?", (MIN_NARRATIVE,)
    ).fetchone()[0]
    with_date = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_accidents WHERE event_date IS NOT NULL"
    ).fetchone()[0]
    dups = c.execute(
        "SELECT COUNT(*) FROM (SELECT case_id, COUNT(*) AS n FROM aaiibcy_accidents GROUP BY case_id HAVING n > 1)"
    ).fetchone()[0]
    scanned = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_reports WHERE source_tier='scanned'"
    ).fetchone()[0]
    ocr = c.execute(
        "SELECT COUNT(*) FROM aaiibcy_reports WHERE source_tier='ocr'"
    ).fetchone()[0]

    print(f"""
=== aaiibcy VERIFY ===
  accidents total   : {total}
  narrative ≥300    : {with_narr}
  dated             : {with_date}
  duplicates        : {dups}
  scanned (no OCR)  : {scanned}
  OCR'd             : {ocr}
""")
    print("Case ID samples:")
    for r in c.execute("SELECT case_id, event_date, registration, aircraft FROM aaiibcy_accidents ORDER BY event_date").fetchall():
        print(f"  {r['case_id']:30s} | {str(r['event_date']):12s} | {str(r['registration']):8s} | {r['aircraft'] or '?'}")


# ---- main -------------------------------------------------------------------

STAGES = {
    "discover": lambda c: discover(c),
    "fetch":    lambda c: fetch_pdfs(c),
    "parse":    lambda c: parse(c),
    "build":    lambda c: build(c),
    "verify":   lambda c: verify(c),
    "all": None,
}

def main():
    os.makedirs(PDFDIR, exist_ok=True)
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage not in STAGES:
        print(f"Usage: {sys.argv[0]} [{' | '.join(STAGES)}]", file=sys.stderr)
        sys.exit(1)

    c = conn()
    if stage == "all":
        discover(c)
        fetch_pdfs(c)
        parse(c)
        build(c)
        verify(c)
    else:
        STAGES[stage](c)
    c.close()

if __name__ == "__main__":
    main()
