#!/usr/bin/env python3
"""KBSZ Hungarian-language aviation report ingest — live kbsz.hu via hetzner egress.

This script adds HU-language PDFs from the live kbsz.hu site to the existing
kbsz.db.  It is additive-only: never modifies existing rows.

Live site URL pattern: https://kbsz.hu/dokumentumok/{filename}.pdf
(NOT the /j25/dokumentumok/ path used for Wayback EN corpus.)

Aviation filter: all PDFs enumerated from the aviation year pages
(/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/NNNN) — rail and water
transport pages were NOT scraped.

Run from minipc (where kbsz.db lives), with OCR_REMOTE=<ocr-host>
set in the environment so OCR runs on hetzner:

  OCR_REMOTE=<ocr-host> python3 kbsz_hu_ingest.py [stage]

Stages:
  discover   — enumerate new HU PDFs from live site (via hetzner proxy)
  fetch      — download PDFs directly from kbsz.hu via hetzner (scp-based)
  parse      — extract text, date, registration, aircraft from local PDFs
  parse-ocr  — OCR fallback for no-text rows
  build      — write kbsz_accidents
  all        — discover+fetch+parse+build (default)
  stats      — print stats only
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile, uuid

HOME = os.path.expanduser("~/kbsz-ingest")
DB = os.path.join(HOME, "kbsz.db")
PDFDIR = os.path.join(HOME, "pdfs")

KBSZ_LIVE_BASE = "https://kbsz.hu"
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.
HETZNER_HOST = os.environ.get("OCR_REMOTE", "")

# Aviation year pages on the HU listing
# These are AVIATION-ONLY pages (légi közlekedés section)
AVIATION_YEAR_PAGES = [
    (2024, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2478"),
    (2023, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2457"),
    (2022, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2415"),
    (2021, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2376"),
    (2020, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2370"),
    (2019, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2367"),
    (2018, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2327"),
    (2017, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2226"),
    (2016, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2164"),
    (2015, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/2029"),
    (2014, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/1891"),
    (2013, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/1753"),
    (2012, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/1644"),
    (2011, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/1483"),
    (2010, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/1239"),
    (2009, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/286"),
    (2008, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/229"),
    (2007, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/141"),
    (2006, "https://kbsz.hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek/64"),
]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DELAY = 2.0
FLOOR = 300  # narrative floor for kbsz_accidents
OCR_LANG = "hun+eng"

# Files that are NOT actual reports — skip them
SKIP_PATTERNS = [
    "lezaro", "lezaras", "lezarasa", "lezarolevel", "lezao",
    "_melleklet", "-melleklet",
    "safety_recommendation",
    "biztonsagi_ajanlas",
    "idkny",    # interim notification document (nem zárójelentés)
    "idokozi_nyilatkozat",  # interim statement
    "_1a",      # safety recommendation variants
    "fuggelek",  # annex
    "lezaro_level",
    "eset_lezarasa",
    "lezao_level",
]


# ---- shared helpers (copied from kbsz_scraper.py) ---------------------------

HU_MONTHS = {
    "január": 1, "januárban": 1, "januárját": 1,
    "február": 2, "februárban": 2,
    "március": 3, "márciusban": 3, "márciusán": 3,
    "április": 4, "áprilisban": 4,
    "május": 5, "májusban": 5,
    "június": 6, "júniusban": 6,
    "július": 7, "júliusban": 7,
    "augusztus": 8, "augusztusban": 8,
    "szeptember": 9, "szeptemberben": 9,
    "október": 10, "októberben": 10,
    "november": 11, "novemberben": 11,
    "december": 12, "decemberben": 12,
}
_HU_MONTH_PAT = re.compile(
    r"\b(\d{4})\.\s+(" + "|".join(sorted(HU_MONTHS, key=len, reverse=True)) + r")\s+(\d{1,2})\.",
    re.IGNORECASE,
)
EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_EN_MONTH_ALT = "|".join(sorted(EN_MONTHS, key=len, reverse=True))
_EN_DATE_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\.?\s+(" + _EN_MONTH_ALT + r")[,.\s]+(\d{4})\b",
    re.IGNORECASE,
)
_EN_DATE_MONTH_DAY_YEAR = re.compile(
    r"\b(" + _EN_MONTH_ALT + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_LEGAL_CONTEXT_RE = re.compile(
    r"(?:Directive|Regulation|Law|Act|Annex|Decree|Order|No\.)\s+[\w./]+\s+of\s+\d",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[.\-/]([01]?\d)[.\-/]((?:19|20)\d{2})\b"
)
_REG_RE = re.compile(r"\b(HA-[A-Z0-9]{2,5}|[A-Z]{1,2}-[A-Z0-9]{2,5})\b")

_HU_WORDS = {
    "repülő", "repülőgép", "leszállás", "felszállás", "baleset", "esemény",
    "vizsgálat", "légijármű", "pilóta", "repülőtér", "következtetés", "okok",
    "helyszín", "sérülés", "személyzet", "műszaki", "üzemeltető", "megállapítás",
    "típusa", "bejelentett", "összefoglaló", "értesítés", "jármű",
}

def detect_lang(txt):
    if not txt:
        return "en"
    words = set(re.findall(r"[a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ]{4,}", txt.lower()))
    if len(words & _HU_WORDS) >= 3:
        return "hu"
    return "en"

def now():
    return int(time.time() * 1000)

def conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c

def _en_month_num(month_str):
    key = month_str.lower()
    if key in EN_MONTHS:
        return EN_MONTHS[key]
    for k in sorted(EN_MONTHS, key=len, reverse=True):
        if key.startswith(k):
            return EN_MONTHS[k]
    return None

def parse_date(txt):
    if not txt:
        return None
    raw_header = txt[:2000]
    header = _LEGAL_CONTEXT_RE.sub("", raw_header)
    m = _EN_DATE_DAY_MONTH_YEAR.search(header)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _en_month_num(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _EN_DATE_MONTH_DAY_YEAR.search(header)
    if m:
        month_str, d, y = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        mo = _en_month_num(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _HU_MONTH_PAT.search(header)
    if m:
        y, month_str, d = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(HU_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = HU_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _DATE_RE.search(header)
    if not m:
        return None
    if m.group(1):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def parse_registration(txt):
    if not txt:
        return None
    header = txt[:3000]
    m = re.search(r"\bHA-[A-Z0-9]{2,5}\b", header)
    if m:
        return m.group(0)
    m = _REG_RE.search(header)
    return m.group(1) if m else None

def parse_aircraft(txt):
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Aircraft\s+type[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"Type of aircraft[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"aircraft[,\s]+([A-Za-z][\w\s\-/]{2,35}),?\s+registration",
        r"\btype[:\s]+([A-Za-z0-9][\w\s\-/]{2,35})[,\n]",
        r"(?:típusa|típus)[:\s]+([A-Za-z0-9][\w\s\-/]{2,35})",
        r"Légijármű\s+típusa[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 50:
                return v
    return None

def parse_location(txt):
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Location[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"Place of occurrence[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"Occurrence site[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"near\s+([A-Z][a-z]{2,}[\w\s,]{0,40}),?\s+Hungary",
        r"(?:Helyszín|Az esemény helye)[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"Esemény helye[:\s]+([A-Za-záéíóöőúüű][\w\s\-,/]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 3 < len(v) < 80:
                return v
    return None

def parse_operator(txt):
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Operator[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"Owner[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"Üzemben tartó[:\s]+([A-Za-záéíóöőúüű][\w\s\-,./]{3,60})",
        r"Üzemeltető[:\s]+([A-Za-záéíóöőúüű][\w\s\-,./]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 80:
                return v
    return None

def parse_probable_cause(txt):
    if not txt:
        return None
    for header_pat in [
        r"(?:CONCLUSIONS?|CAUSES?|PROBABLE\s+CAUSE)[\s:]*\n(.*?)(?=\n[A-Z]{3}|\Z)",
        r"(?:Conclusions?|Causes?|Probable cause)[\s:]*\n(.*?)(?=\n[A-Z]{3}|\Z)",
        r"(?:KÖVETKEZTETÉSEK|OKOK|MEGÁLLAPÍTÁSOK)[\s:]*\n(.*?)(?=\n[A-ZÁÉÍÓÚ]{3}|\Z)",
        r"(?:Következtetések|Okok|Megállapítások)[\s:]*\n(.*?)(?=\n[A-ZÁÉÍÓÚA-Z]{4}|\Z)",
    ]:
        m = re.search(header_pat, txt, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1).strip()
            section = re.split(r"\n(?:[A-Z][A-Z\s]{5,}|[0-9]+\.)\s*\n", section)[0]
            section = section[:2000].strip()
            if len(section) > 50:
                return section
    return None

def extract_text(path):
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


# ---- case_id extraction -------------------------------------------------------

def case_id_from_path(path):
    """Extract clean case_id from a kbsz.hu PDF path.

    Returns (case_id, is_skip) where is_skip=True for closure letters, annexes etc.
    """
    fn = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
    fn_lower = fn.lower()
    fn_no_ext = re.sub(r'\.pdf$', '', fn_lower, flags=re.I)

    # Skip non-reports
    for pat in SKIP_PATTERNS:
        if pat in fn_no_ext:
            return None, True

    # Pattern 1: YYYY-NNN(N)-N standard case e.g. 2007-196-4, 2009-023-4, 2016-0505-4
    m = re.search(r'(\d{4})-(\d{3,4})-(\d+)', fn_no_ext)
    if m:
        year, num, mode = m.group(1), m.group(2), m.group(3)
        # Normalise mode: remove trailing P from 4p/5p/6p
        mode_clean = re.sub(r'[p]+$', '', mode, flags=re.I)
        return f"{year}-{num}-{mode_clean}", False

    # Pattern 2: zj-07-202 → 2007-202
    m = re.match(r'zj-0?(\d)-(\d{3,4})$', fn_no_ext)
    if m:
        d = int(m.group(1))
        year = f"200{d}" if d >= 6 else f"201{d}"
        return f"{year}-{m.group(2)}", False

    # Pattern 3: 2009 210 ZJ → 2009-210 (spaces become part of filename)
    fn_spaced = fn_no_ext.replace('%20', ' ')
    m = re.match(r'(\d{4})\s+[-\s]*(\d{3,4})', fn_spaced)
    if m:
        return f"{m.group(1)}-{m.group(2)}", False

    # Pattern 4: zj 2008-282-4p → already covered by pattern 1 mostly
    # But handle leading "zj " prefix
    fn_strip = re.sub(r'^zj\s+', '', fn_no_ext.strip())
    m = re.search(r'(\d{4})-(\d{3,4})-(\d+)', fn_strip)
    if m:
        year, num, mode = m.group(1), m.group(2), m.group(3)
        mode_clean = re.sub(r'[p]+$', '', mode, flags=re.I)
        return f"{year}-{num}-{mode_clean}", False

    # Pattern 5: bare YYYY-NNN or YYYY-NNNN without mode digit
    m = re.match(r'^(\d{4})-(\d{3,4})\b', fn_no_ext)
    if m:
        return f"{m.group(1)}-{m.group(2)}", False

    # Pattern 6: 09-259 → 2009-259
    m = re.match(r'^0?(\d)-(\d{3,4})', fn_no_ext)
    if m:
        d = int(m.group(1))
        year = f"200{d}" if d >= 6 else f"201{d}"
        return f"{year}-{m.group(2)}", False

    # Pattern 7: 283-4-2009.pdf → 2009-283-4
    m = re.match(r'^(\d{3,4})-(\d+)-(\d{4})', fn_no_ext)
    if m:
        return f"{m.group(3)}-{m.group(1)}-{m.group(2)}", False

    return None, False


def lang_from_path(path):
    """Infer language from URL path."""
    fn = urllib.parse.unquote(path).split("/")[-1].lower()
    if re.search(r'_zj_hu\b|_hu\b|_hu_|zj\s+hu\b|magyar', fn):
        return "hu"
    if re.search(r'_(?:zj_)?en\b|_fr_en\b|_ir.en\b|final.report|english|engsum|-1a\b|_en\.pdf', fn):
        return "en"
    return "hu"  # default to HU for live site PDFs without explicit en marker


# ---- remote fetch via hetzner ------------------------------------------------

def fetch_pdf_via_hetzner(live_url, dest_path):
    """Download a PDF from kbsz.hu via hetzner (DE IP), save locally.

    Returns True on success, False on failure.
    Strategy: ssh to hetzner, curl the URL to a tmp file, scp back.
    """
    remote_tmp = "/tmp/kbsz-dl-%s.pdf" % uuid.uuid4().hex

    try:
        # Download on hetzner — use args list to avoid shell quoting issues
        result = subprocess.run(
            ["ssh", HETZNER_HOST, "curl", "-sk", "--max-time", "120",
             "-o", remote_tmp, live_url],
            capture_output=True, timeout=180,
        )
        if result.returncode != 0:
            return False

        # Check file size on hetzner (avoids complex shell quoting)
        size_check = subprocess.run(
            ["ssh", HETZNER_HOST, "stat", "-c", "%s", remote_tmp],
            capture_output=True, timeout=15,
        )
        try:
            remote_size = int(size_check.stdout.strip())
        except (ValueError, AttributeError):
            remote_size = 0
        if remote_size < 500:
            subprocess.run(["ssh", HETZNER_HOST, "rm", "-f", remote_tmp],
                           capture_output=True, timeout=15)
            return False

        # scp back to minipc
        scp = subprocess.run(
            ["scp", "-q", "%s:%s" % (HETZNER_HOST, remote_tmp), dest_path],
            capture_output=True, timeout=180,
        )
        # cleanup remote tmp
        subprocess.run(["ssh", HETZNER_HOST, "rm", "-f", remote_tmp],
                       capture_output=True, timeout=15)
        if scp.returncode != 0:
            return False
        # Final verify magic bytes locally
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 500:
            with open(dest_path, "rb") as fh:
                return fh.read(4) == b"%PDF"
        return False
    except subprocess.TimeoutExpired:
        subprocess.run(["ssh", HETZNER_HOST, "rm", "-f", remote_tmp],
                       capture_output=True, timeout=15)
        return False


# ---- discover ---------------------------------------------------------------

def discover(c):
    """Enumerate HU PDFs from aviation year pages on live kbsz.hu via hetzner.

    Inserts new rows into kbsz_reports with status='hu_new'.
    Skips case_ids already present (to preserve EN corpus intact).
    """
    # Get all existing case_ids
    existing = {r[0] for r in c.execute("SELECT case_id FROM kbsz_reports")}
    print(f"[discover] existing kbsz_reports: {len(existing)}", flush=True)

    # Build lookup: base-id (YYYY-NNN) -> full case_id (YYYY-NNN-4) for EN rows.
    # Used to detect HU bare-id (2009-229) that is a semantic dup of EN -4 variant.
    # Keep-one invariant: if a match is found, mark HU as semantic-dup and
    # NEVER touch the EN entry in kbsz_accidents.
    _mode_strip_re = re.compile(r"-\d+$")
    en_base_to_cid = {}
    for (row_cid, row_lang) in c.execute("SELECT case_id, lang FROM kbsz_reports"):
        if row_lang == "en":
            base = _mode_strip_re.sub("", row_cid)
            if base != row_cid:  # had a mode digit to strip
                en_base_to_cid[base] = row_cid

    inserted = 0
    skipped_dup = 0
    skipped_non_report = 0

    for year, page_url in AVIATION_YEAR_PAGES:
        print(f"[discover] fetching {year} page ...", flush=True)
        # Fetch via hetzner using args list (no shell quoting issues)
        # kbsz.hu works from DE without special headers
        result = subprocess.run(
            ["ssh", HETZNER_HOST,
             "curl", "-sk", "--max-time", "30", page_url],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"  [discover] ssh failed for {year}", file=sys.stderr, flush=True)
            continue

        html = result.stdout.decode("utf-8", "replace")
        # Extract PDF hrefs
        pdf_hrefs = re.findall(
            r'href="(/dokumentumok/[^"]+\.pdf[^"]*)"',
            html, re.IGNORECASE,
        )
        print(f"  {year}: {len(pdf_hrefs)} PDF links", flush=True)

        seen_in_page = set()
        for raw_href in pdf_hrefs:
            href = urllib.parse.unquote(raw_href)
            href_lower = href.lower()
            fn = href.split("/")[-1]
            fn_lower = fn.lower()

            cid, is_skip = case_id_from_path(raw_href)
            if is_skip or cid is None:
                skipped_non_report += 1
                continue

            # Deduplicate within page: prefer HU over EN variant of same case
            is_en = re.search(r'_(?:zj_)?en\b|_fr_en\b|english|en_sign|final.report', fn_lower)
            if cid in seen_in_page:
                # Already have this case from this page — keep HU preference
                if is_en:
                    continue  # skip EN duplicate if we already have something
            seen_in_page.add(cid)

            # Semantic-dup guard: if this bare HU case (e.g. 2009-229) corresponds
            # to an EN case already stored with a mode-digit suffix (e.g. 2009-229-4),
            # mark it as semantic-dup and skip. Keep-one invariant: NEVER delete the
            # EN entry from kbsz_accidents — at least one variant must always survive.
            if cid not in existing:
                _base = _mode_strip_re.sub("", cid)
                _paired_en = en_base_to_cid.get(_base) or en_base_to_cid.get(cid)
                if _paired_en:
                    _live_url_tmp = KBSZ_LIVE_BASE + raw_href
                    _lang_tmp = lang_from_path(raw_href)
                    c.execute(
                        "INSERT OR IGNORE INTO kbsz_reports "
                        "(case_id, source_url, anchor_text, report_type, lang, "
                        " status, skip_reason, discovered_at, updated_at) "
                        "VALUES (?, ?, ?, 'Final report', ?, 'skipped', 'semantic-dup', ?, ?)"
                        ,
                        (cid, _live_url_tmp, fn, _lang_tmp, now(), now()),
                    )
                    c.commit()
                    existing.add(cid)
                    print(
                        f"  [discover] {cid} -> semantic-dup of {_paired_en} (EN already built)",
                        flush=True,
                    )
                    skipped_dup += 1
                    continue

            if cid in existing:
                skipped_dup += 1
                continue

            live_url = KBSZ_LIVE_BASE + raw_href
            lang = lang_from_path(raw_href)

            c.execute(
                "INSERT OR IGNORE INTO kbsz_reports "
                "(case_id, source_url, anchor_text, report_type, lang, status, discovered_at, updated_at) "
                "VALUES (?, ?, ?, 'Final report', ?, 'hu_new', ?, ?)",
                (cid, live_url, fn, lang, now(), now()),
            )
            c.commit()
            existing.add(cid)  # prevent dups across year pages
            inserted += 1

        time.sleep(DELAY)

    print(f"[discover] inserted={inserted} skipped_dup={skipped_dup} skipped_non_report={skipped_non_report}", flush=True)
    return inserted


# ---- fetch ------------------------------------------------------------------

def fetch(c):
    """Download PDFs via hetzner for rows with status='hu_new'."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='hu_new'"
    ).fetchall()
    print(f"[fetch] {len(rows)} rows to download", flush=True)

    downloaded = 0
    failed = 0

    for row in rows:
        cid = row["case_id"]
        live_url = row["source_url"]
        safe_name = "hu-" + re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[fetch] {cid} ...", flush=True)

        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as fh:
                if fh.read(4) == b"%PDF":
                    print(f"  already on disk", flush=True)
                    c.execute(
                        "UPDATE kbsz_reports SET pdf_path=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                        (dest, now(), cid),
                    )
                    c.commit()
                    downloaded += 1
                    continue

        ok = fetch_pdf_via_hetzner(live_url, dest)
        if ok:
            size_kb = os.path.getsize(dest) // 1024
            print(f"  saved {size_kb}KB", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, status='hu_fetched', updated_at=? WHERE case_id=?",
                (dest, live_url, now(), cid),
            )
            c.commit()
            downloaded += 1
        else:
            print(f"  FAILED: {live_url}", file=sys.stderr, flush=True)
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason='fetch-failed', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            failed += 1

        time.sleep(DELAY)

    print(f"[fetch] downloaded={downloaded} failed={failed}", flush=True)
    return downloaded, failed


# ---- parse ------------------------------------------------------------------

def parse(c):
    """Extract text + metadata from hu_fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, lang FROM kbsz_reports WHERE status='hu_fetched'"
    ).fetchall()
    print(f"[parse] {len(rows)} rows to parse", flush=True)
    parsed = 0
    no_text = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[parse] {cid}", flush=True)

        txt = extract_text(pdf_path)
        char_cnt = len(txt)

        if char_cnt < 80:
            print(f"  only {char_cnt} chars → no-text", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET narrative_text=?, status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (txt, now(), cid),
            )
            c.commit()
            no_text += 1
            continue

        lang_detected = detect_lang(txt)
        event_date = parse_date(txt)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        probable_cause = parse_probable_cause(txt)

        c.execute(
            """UPDATE kbsz_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?, lang=?,
                 status='hu_parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft, location,
             lang_detected, now(), cid),
        )
        c.commit()
        print(f"  {char_cnt}ch date={event_date} reg={registration} lang={lang_detected}", flush=True)
        parsed += 1

    print(f"[parse] parsed={parsed} no_text={no_text}", flush=True)
    return parsed, no_text


# ---- parse-ocr --------------------------------------------------------------

def parse_ocr(c):
    """OCR fallback for no-text HU PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path FROM kbsz_reports "
        "WHERE status='skipped' AND skip_reason='no-text' "
        "AND source_url LIKE '%kbsz.hu/dokumentumok%'"
    ).fetchall()
    print(f"[parse-ocr] {len(rows)} HU no-text rows", flush=True)
    ocr_ok = 0
    still_blank = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  [parse-ocr] {cid}: PDF missing", file=sys.stderr, flush=True)
            still_blank += 1
            continue
        print(f"  [parse-ocr] OCR {cid} ...", flush=True)
        txt = ocr_extract(pdf_path, lang=OCR_LANG)
        char_cnt = len(txt)
        print(f"    → {char_cnt} chars", flush=True)
        if char_cnt < 80:
            still_blank += 1
            c.execute(
                "UPDATE kbsz_reports SET narrative_text=?, updated_at=? WHERE case_id=?",
                (txt, now(), cid),
            )
            c.commit()
            continue
        lang_detected = detect_lang(txt)
        event_date = parse_date(txt)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        probable_cause = parse_probable_cause(txt)
        c.execute(
            """UPDATE kbsz_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?, lang=?,
                 status='hu_parsed', skip_reason=NULL, updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft, location,
             lang_detected, now(), cid),
        )
        c.commit()
        ocr_ok += 1
        print(f"    date={event_date} reg={registration} lang={lang_detected}", flush=True)

    print(f"[parse-ocr] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank


# ---- build ------------------------------------------------------------------

def build(c):
    """Write kbsz_accidents from hu_parsed rows (FLOOR=300 chars)."""
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM kbsz_reports WHERE status='hu_parsed'"""
    ).fetchall()
    print(f"[build] {len(rows)} hu_parsed rows", flush=True)
    built = 0
    too_short = 0

    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            print(f"  [build] {r['case_id']}: narrative {len(narr)} < {FLOOR} → skip", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason='short-narrative', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            too_short += 1
            continue

        # kbsz-- prefix for global uniqueness (same convention as original scraper)
        cid = "kbsz-" + r["case_id"]
        slug = cid.lower()
        operator = parse_operator(narr)

        # Safety check: do not overwrite any existing row.
        # Keep-one invariant: INSERT (not INSERT OR REPLACE) — this build() function
        # NEVER deletes from kbsz_accidents. If an EN variant with the same case_id
        # already exists, we skip silently. The semantic-dup guard in discover()
        # ensures we never even fetch the HU variant in that case.
        exists = c.execute(
            "SELECT case_id FROM kbsz_accidents WHERE case_id=?", (cid,)
        ).fetchone()
        if exists:
            print(f"  [build] {cid} already in kbsz_accidents — skip", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET status='built', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        c.execute(
            """INSERT INTO kbsz_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'HU', ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                r["event_date"],
                r["aircraft"],
                r["registration"],
                operator,
                r["location"],
                narr,
                r["probable_cause"],
                r["source_url"],
                r["report_type"] or "Final report",
                slug,
                r["lang"] or "hu",
                now(),
            ),
        )
        c.execute(
            "UPDATE kbsz_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[build] built={built} too_short={too_short}", flush=True)
    return built, too_short


# ---- stats ------------------------------------------------------------------

def print_stats(c):
    print("\n=== kbsz_reports status breakdown ===")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason ORDER BY n DESC"
    ):
        print(f"  {row['status']:15s}  {(row['skip_reason'] or ''):25s}  {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM kbsz_accidents").fetchone()[0]
    print(f"\n=== kbsz_accidents total: {cnt} rows ===")
    if cnt:
        row = c.execute(
            "SELECT SUM(lang='en'), SUM(lang='hu'), "
            "MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM kbsz_accidents"
        ).fetchone()
        print(f"  lang: en={row[0]} hu={row[1]}  narr_len min={row[2]} max={row[3]}")
        null_dates = c.execute("SELECT COUNT(*) FROM kbsz_accidents WHERE event_date IS NULL").fetchone()[0]
        print(f"  event_date NULL: {null_dates} ({100*null_dates//cnt}%)")
        print("\n  sample HU rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, lang, LENGTH(narrative_text) len "
            "FROM kbsz_accidents WHERE lang='hu' ORDER BY case_id LIMIT 10"
        ):
            print(f"    {r['case_id']:25s}  reg={r['registration'] or 'NULL':10s}  date={r['event_date'] or 'NULL'}  lang={r['lang']}  narr={r['len']}")

    # duplicate check
    dups = c.execute(
        "SELECT case_id, count(*) n FROM kbsz_accidents GROUP BY case_id HAVING n > 1"
    ).fetchall()
    if dups:
        print(f"\n  WARN: {len(dups)} duplicate case_ids in kbsz_accidents!")
        for d in dups:
            print(f"    {d['case_id']}: {d['n']}")
    else:
        print("\n  case_id uniqueness: OK")


# ---- main -------------------------------------------------------------------

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

    if mode == "parse-ocr":
        parse_ocr(c)
        build(c)

    if mode in ("build", "all"):
        build(c)

    print_stats(c)


if __name__ == "__main__":
    main()
