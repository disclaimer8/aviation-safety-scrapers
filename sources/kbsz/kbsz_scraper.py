#!/usr/bin/env python3
"""KBSZ (Hungary — Közlekedésbiztonsági Szervezet) aviation-accident ingest.

Source: kbsz.hu official investigation reports — recovered ENTIRELY from the
Wayback Machine because live kbsz.hu geo-blocks all non-Hungarian IPs (BigIP F5).
DO NOT probe kbsz.hu directly.

Listing page (EN):
  https://www.kbsz.hu/j25/en/aviation/occurrences-investigated
  → captured at archive.org, fetched via id_ flag to get original bytes.

PDF URL patterns:
  Newer (2014–2024): /j25/dokumentumok/{YYYY}-{NNN(N)}-{N}[P]_{suffix}.pdf
    suffixes: _Final_report, _FR_EN, _ZJ_EN, _IR-EN, _FR, _ZJ, _ZJ_HU, …
  Older EN translations (2006–2013): /j25/dokumentumok/eng/{YYYY}-{NNN}-{N}_en.pdf

case_id format: {YYYY}-{NNN or NNNN}-{transport-mode digit}
  aviation mode digits: 4 / 5 / 6
  Some older cases omit the mode digit (e.g. 2015-263, 2014-402).
  These are stored as-is (e.g. "2015-263") — still stable, intrinsic IDs.

Stages: discover | fetch | parse | build | recheck | all  (via argv[1])
  recheck: re-queries CDX for previously not-archived PDFs; no full re-crawl.

Politeness: 2 s base delay, exponential backoff on 429/503 (30–60 s, 3 retries).
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile, uuid

KBSZ_BASE = "https://www.kbsz.hu"
EN_LISTING = KBSZ_BASE + "/j25/en/aviation/occurrences-investigated"
HU_LISTING = KBSZ_BASE + "/j25/hu/legi-kozlekedes/kbsz-altal-vizsgalt-esemenyek"

# Most recent, largest Wayback snapshot for the EN listing (20250319 = 10495 bytes, 117 links)
EN_LISTING_TS = "20250319171441"
# HU listing snapshots don't include direct PDF links (Joomla renders them
# on individual sub-pages); the EN listing is comprehensive enough.
# We do NOT waste archive.org quota on the HU listing since it adds nothing.

WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 2.0        # base inter-request delay
FLOOR = 80         # minimum chars to consider text usable
HOME = os.path.expanduser("~/kbsz-ingest")
DB = os.path.join(HOME, "kbsz.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

OCR_LANG = "hun+eng"  # tesseract: hun installed on hetzner 2026-06-09, eng always present

# ---- OCR helpers (adapted from aaid-ingest/aaid_ingest/pdf.py) ---------------

def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote host via ssh.  Returns "" on any failure."""
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
    """OCR a scanned PDF; returns recognised text or "" on failure.

    Prefers OCR_REMOTE env (<ocr-host>) to run on hetzner,
    keeping heavy OCR off the loaded mini-PC.  Graceful 600 s local timeout.
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


# Hungarian word list for lang detection — presence of 3+ unique tokens → 'hu'
_HU_WORDS = {
    "repülő", "repülőgép", "leszállás", "felszállás", "baleset", "esemény",
    "vizsgálat", "légijármű", "pilóta", "repülőtér", "következtetés", "okok",
    "helyszín", "sérülés", "személyzet", "műszaki", "üzemeltető", "megállapítás",
    "típusa", "bejelentett", "összefoglaló", "értesítés", "jármű",
}

def detect_lang(txt):
    """Return 'hu' if text is predominantly Hungarian, else 'en'."""
    if not txt:
        return "en"
    words = set(re.findall(r"[a-záéíóöőúüűA-ZÁÉÍÓÖŐÚÜŰ]{4,}", txt.lower()))
    if len(words & _HU_WORDS) >= 3:
        return "hu"
    return "en"



SCHEMA = """
CREATE TABLE IF NOT EXISTS kbsz_reports (
  case_id      TEXT PRIMARY KEY,
  source_url   TEXT,        -- original kbsz.hu PDF URL (canonical attribution)
  archive_url  TEXT,        -- Wayback archive snapshot URL (provenance)
  archive_ts   TEXT,        -- CDX timestamp of the snapshot used
  pdf_path     TEXT,
  anchor_text  TEXT,
  report_type  TEXT,
  aircraft     TEXT,
  registration TEXT,
  event_date   TEXT,
  location     TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang         TEXT DEFAULT 'en',
  status       TEXT DEFAULT 'new',
  skip_reason  TEXT,
  discovered_at INT,
  updated_at   INT
);
CREATE TABLE IF NOT EXISTS kbsz_accidents (
  case_id       TEXT PRIMARY KEY,
  event_date    TEXT,
  aircraft      TEXT,
  registration  TEXT,
  operator      TEXT,
  location      TEXT,
  country       TEXT DEFAULT 'HU',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url    TEXT,
  report_type   TEXT,
  site_slug     TEXT,
  lang          TEXT DEFAULT 'en',
  built_at      INT
);
CREATE INDEX IF NOT EXISTS idx_kbsz_status ON kbsz_reports(status);
"""

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
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_EN_MONTH_ALT = "|".join(sorted(EN_MONTHS, key=len, reverse=True))
# EN: "05. June, 2022" or "14 May 2022" or "14th May 2022"
_EN_DATE_DAY_MONTH_YEAR = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\.?\s+(" + _EN_MONTH_ALT + r")[,.\s]+(\d{4})\b",
    re.IGNORECASE,
)
# EN: "June 5, 2022" or "June 5th, 2022"
_EN_DATE_MONTH_DAY_YEAR = re.compile(
    r"\b(" + _EN_MONTH_ALT + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
# Legal-context exclusion: skip matches preceded by "Directive", "Regulation", "Law", "Act", "No."
_LEGAL_CONTEXT_RE = re.compile(
    r"(?:Directive|Regulation|Law|Act|Annex|Decree|Order|No\.)\s+[\w./]+\s+of\s+\d",
    re.IGNORECASE,
)
# ISO / numeric date: YYYY-MM-DD or DD.MM.YYYY or DD/MM/YYYY or YYYY.MM.DD
_DATE_RE = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[.\-/]([01]?\d)[.\-/]((?:19|20)\d{2})\b"
)

_REG_RE = re.compile(r"\b(HA-[A-Z0-9]{2,5}|[A-Z]{1,2}-[A-Z0-9]{2,5})\b")

def now():
    return int(time.time() * 1000)

def conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def http():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=60.0,
        follow_redirects=True,
    )

def wayback_get(cl, url, retries=3):
    """GET with exponential backoff for Wayback 429/503."""
    delay = DELAY
    for attempt in range(retries):
        try:
            r = cl.get(url)
            if r.status_code in (429, 503):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] {r.status_code} → sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            time.sleep(delay)
            return r
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(30)
            else:
                raise
    return None

def case_id_from_url(href):
    """Extract clean case_id from KBSZ PDF href."""
    fn = urllib.parse.unquote(href.rstrip("/").split("/")[-1])
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I)
    # sr ba2017-371-4-1a (named report variant)
    m = re.search(r"(\d{4})-(\d{3,4})-(\d)", fn)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # e.g. 2010-004-zj_en → no mode digit, just year-nnn
    m = re.match(r"^(\d{4})-(\d{3,4})", fn)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-") or None

def lang_from_url(href):
    """Infer language from URL path (all links on EN listing → EN by default)."""
    fn = urllib.parse.unquote(href).split("/")[-1].lower()
    # Explicit HU-only markers
    if re.search(r"_zj_hu\b|_hu\b", fn):
        return "hu"
    # Explicit EN markers or /eng/ sub-dir
    if "/eng/" in href.lower():
        return "en"
    if re.search(r"_(?:zj_)?en\b|_fr_en\b|_ir.en\b|final.report|english|engsum|fr_engsum|-1a\b", fn):
        return "en"
    # FR/ZJ without lang tag — these appear on the EN listing → EN
    return "en"

def report_type_from_url(href):
    fn = urllib.parse.unquote(href).split("/")[-1].lower()
    if "interim" in fn or "_ir" in fn:
        return "Interim report"
    if "final" in fn or "_zj" in fn or "_fr" in fn:
        return "Final report"
    return "Final report"

def _en_month_num(month_str):
    """Resolve EN month string to integer 1-12."""
    key = month_str.lower()
    if key in EN_MONTHS:
        return EN_MONTHS[key]
    for k in sorted(EN_MONTHS, key=len, reverse=True):
        if key.startswith(k):
            return EN_MONTHS[k]
    return None

def _strip_legal(txt):
    """Remove legal reference sentences to avoid false date matches.

    Sentences like 'Council Directive 94/56/EC of 21 November 1994' contain
    historical dates that are NOT the occurrence date.
    """
    return _LEGAL_CONTEXT_RE.sub("", txt)

def parse_date(txt):
    """Parse occurrence date from PDF text. Returns ISO YYYY-MM-DD or None.

    Priority (search first 2000 chars, legal refs stripped):
    1. EN ordinal/plain day-month-year: "15th July 2006", "14 May 2022", "05. June, 2022"
    2. EN month-day-year: "June 5, 2022"
    3. Hungarian written month: "2016. március 5."
    4. ISO/numeric: YYYY-MM-DD, DD.MM.YYYY, etc.

    We restrict to the first 2000 chars (report header) to avoid picking up
    dates from appendices or references sections.
    """
    if not txt:
        return None
    raw_header = txt[:2000]
    header = _strip_legal(raw_header)

    # 1. EN day-month-year (with optional ordinal): "15th July 2006", "14 May 2022"
    m = _EN_DATE_DAY_MONTH_YEAR.search(header)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _en_month_num(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # 2. EN month-day-year: "June 5, 2022"
    m = _EN_DATE_MONTH_DAY_YEAR.search(header)
    if m:
        month_str, d, y = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        mo = _en_month_num(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # 3. Hungarian written month: 2016. március 5.
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

    # 4. ISO / numeric (also stripped of legal refs)
    m = _DATE_RE.search(header)
    if not m:
        return None
    if m.group(1):  # YYYY-MM-DD or YYYY.MM.DD
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:           # DD.MM.YYYY
        d, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def parse_registration(txt):
    """Extract aircraft registration (prefer HA- prefix)."""
    if not txt:
        return None
    header = txt[:3000]
    m = re.search(r"\bHA-[A-Z0-9]{2,5}\b", header)
    if m:
        return m.group(0)
    m = _REG_RE.search(header)
    return m.group(1) if m else None

def parse_aircraft(txt):
    """Extract aircraft type from PDF text."""
    if not txt:
        return None
    header = txt[:4000]
    # Common EN patterns in KBSZ reports
    for pat in [
        r"Aircraft\s+type[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"Type of aircraft[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"aircraft[,\s]+([A-Za-z][\w\s\-/]{2,35}),?\s+registration",
        r"\btype[:\s]+([A-Za-z0-9][\w\s\-/]{2,35})[,\n]",
        # HU pattern: Légi jármű típusa
        r"(?:típusa|típus)[:\s]+([A-Za-z0-9][\w\s\-/]{2,35})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 50:
                return v
    return None

def parse_location(txt):
    """Extract occurrence location from PDF text."""
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Location[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"Place of occurrence[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"Occurrence site[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"near\s+([A-Z][a-z]{2,}[\w\s,]{0,40}),?\s+Hungary",
        # HU
        r"(?:Helyszín|Az esemény helye)[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 3 < len(v) < 80:
                return v
    return None

def parse_operator(txt):
    """Extract operator/owner from PDF text."""
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Operator[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"Owner[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"Üzemben tartó[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 80:
                return v
    return None

def parse_probable_cause(txt):
    """Extract conclusions/causes section from PDF text."""
    if not txt:
        return None
    # EN sections
    for header_pat in [
        r"(?:CONCLUSIONS?|CAUSES?|PROBABLE\s+CAUSE)[\s:]*\n(.*?)(?=\n[A-Z]{3}|\Z)",
        r"(?:Conclusions?|Causes?|Probable cause)[\s:]*\n(.*?)(?=\n[A-Z]{3}|\Z)",
        # HU
        r"(?:KÖVETKEZTETÉSEK|OKOK)[\s:]*\n(.*?)(?=\n[A-ZÁÉÍÓÚ]{3}|\Z)",
    ]:
        m = re.search(header_pat, txt, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1).strip()
            # truncate at next section header or after ~2000 chars
            section = re.split(r"\n(?:[A-Z][A-Z\s]{5,}|[0-9]+\.)\s*\n", section)[0]
            section = section[:2000].strip()
            if len(section) > 50:
                return section
    return None

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

def cdx_best_snapshot(cl, orig_url):
    """Query CDX API and return best snapshot timestamp (or None)."""
    cdx_url = (
        f"{CDX_BASE}?url={urllib.parse.quote(orig_url, safe=':/')}"
        f"&output=json&filter=statuscode:200&limit=5"
    )
    try:
        r = wayback_get(cl, cdx_url)
        if not r or r.status_code != 200:
            return None
        rows = r.json()
        if len(rows) <= 1:  # only header row
            return None
        # rows[0] is header; pick most recent (last row)
        return rows[-1][1]  # timestamp field
    except Exception as e:
        print(f"  [cdx] error for {orig_url}: {e}", file=sys.stderr)
        return None

# ---- DISCOVER ----------------------------------------------------------------

def discover(c, cl):
    """Fetch EN listing snapshot and populate kbsz_reports."""
    wb_url = f"{WAYBACK_BASE}/{EN_LISTING_TS}id_/{EN_LISTING}"
    print(f"[kbsz discover] fetching {wb_url}", flush=True)
    r = wayback_get(cl, wb_url)
    if not r or r.status_code != 200:
        print(f"[kbsz discover] HTTP {r.status_code if r else 'none'}", file=sys.stderr)
        return 0
    html = r.text

    # Extract (href, anchor_text) pairs
    pairs = []
    seen_urls = set()
    for m in re.finditer(
        r'<a[^>]+href="([^"]*j25/dokumentumok[^"]*\.pdf[^"]*)"[^>]*>(.*?)</a>',
        html, re.I | re.S,
    ):
        href = m.group(1).strip()
        # normalise to absolute kbsz.hu URL (canonical source_url)
        if not href.startswith("http"):
            href = KBSZ_BASE + href
        # deduplicate same URL
        norm = urllib.parse.unquote(href).lower()
        if norm in seen_urls:
            continue
        seen_urls.add(norm)
        anchor = re.sub(r"<[^>]+>", "", m.group(2))
        anchor = re.sub(r"\s+", " ", anchor).strip()
        pairs.append((href, anchor))

    print(f"[kbsz discover] found {len(pairs)} unique PDF links", flush=True)

    inserted = 0
    for href, anchor in pairs:
        cid = case_id_from_url(href)
        if not cid:
            print(f"  [warn] no case_id for {href}", file=sys.stderr)
            continue
        # Dedup by case_id — prefer EN PDF when multiple URLs share same case_id
        existing = c.execute(
            "SELECT case_id, source_url FROM kbsz_reports WHERE case_id=?", (cid,)
        ).fetchone()
        if existing:
            # Keep if current is EN and stored is HU
            old_lang = lang_from_url(existing["source_url"])
            new_lang = lang_from_url(href)
            if old_lang == "en" or new_lang != "en":
                continue  # existing is fine
            # Upgrade to EN version
            c.execute(
                "UPDATE kbsz_reports SET source_url=?, lang=?, updated_at=? WHERE case_id=?",
                (href, "en", now(), cid),
            )
            c.commit()
            continue

        lang = lang_from_url(href)
        rtype = report_type_from_url(href)
        c.execute(
            "INSERT OR IGNORE INTO kbsz_reports "
            "(case_id, source_url, anchor_text, report_type, lang, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
            (cid, href, anchor, rtype, lang, now(), now()),
        )
        c.commit()
        inserted += 1

    print(f"[kbsz discover] inserted {inserted} new rows", flush=True)
    return inserted

# ---- FETCH -------------------------------------------------------------------

def fetch(c, cl):
    """Download PDFs via Wayback Machine CDX → snapshot URL."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    not_archived = []

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[kbsz fetch] {cid} ...", flush=True)

        # Already downloaded?
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            ts_cached = c.execute(
                "SELECT archive_ts FROM kbsz_reports WHERE case_id=?", (cid,)
            ).fetchone()
            if ts_cached and ts_cached["archive_ts"]:
                c.execute(
                    "UPDATE kbsz_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
                downloaded += 1
                continue

        # CDX lookup
        ts = cdx_best_snapshot(cl, orig_url)
        if not ts:
            print(f"  [kbsz fetch] {cid}: not archived", flush=True)
            not_archived.append(cid)
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason='not-archived', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
        print(f"  ts={ts} archive_url={archive_url}", flush=True)

        try:
            r = wayback_get(cl, archive_url)
            if not r or r.status_code != 200:
                print(f"  HTTP {r.status_code if r else 'none'} for {cid}", file=sys.stderr)
                c.execute(
                    "UPDATE kbsz_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (f"http-{r.status_code if r else 'none'}", now(), cid),
                )
                c.commit()
                continue

            # Verify PDF magic bytes
            if r.content[:4] != b"%PDF":
                print(f"  [kbsz fetch] {cid}: not a PDF (got {r.content[:20]!r})", file=sys.stderr)
                c.execute(
                    "UPDATE kbsz_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

            with open(dest, "wb") as fh:
                fh.write(r.content)
            c.execute(
                "UPDATE kbsz_reports SET pdf_path=?, archive_url=?, archive_ts=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, archive_url, ts, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved {len(r.content)//1024}KB", flush=True)

        except Exception as e:
            print(f"  [kbsz fetch] {cid}: {e}", file=sys.stderr)
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                (str(e)[:120], now(), cid),
            )
            c.commit()

    print(f"[kbsz fetch] downloaded={downloaded} not_archived={len(not_archived)}", flush=True)
    if not_archived:
        print(f"  not-archived case IDs: {not_archived}", flush=True)
    return downloaded, not_archived

# ---- PARSE -------------------------------------------------------------------

def parse(c):
    """Extract text + metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, report_type, lang FROM kbsz_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[kbsz parse] {cid}", flush=True)

        txt = extract_text(pdf_path)
        if len(txt) < FLOOR:
            print(f"  [kbsz parse] {cid}: only {len(txt)} chars → skipped (no-text)", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET narrative_text=?, status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (txt, now(), cid),
            )
            c.commit()
            continue

        event_date = parse_date(txt)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)

        c.execute(
            """UPDATE kbsz_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft, location, now(), cid),
        )
        c.commit()
        parsed += 1
    print(f"[kbsz parse] parsed={parsed}", flush=True)
    return parsed

# ---- PARSE-SKIPPED (OCR fallback) -------------------------------------------

def parse_skipped(c):
    """Re-parse no-text rows via OCR (hun+eng tesseract on hetzner).

    Selects kbsz_reports rows with status='skipped', skip_reason='no-text',
    runs ocr_extract (remote via OCR_REMOTE env), updates narrative + metadata,
    and sets status='parsed' so build() picks them up.
    PDF files must already be on disk (they were downloaded during fetch).
    """
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, report_type, lang "
        "FROM kbsz_reports WHERE status='skipped' AND skip_reason='no-text'"
    ).fetchall()
    print(f"[kbsz parse-skipped] {len(rows)} no-text rows to OCR", flush=True)
    ocr_ok = 0
    still_blank = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  [kbsz parse-skipped] {cid}: PDF missing at {pdf_path}", file=sys.stderr, flush=True)
            still_blank += 1
            continue
        print(f"  [kbsz parse-skipped] OCR {cid} ...", flush=True)
        txt = ocr_extract(pdf_path, lang=OCR_LANG)
        char_cnt = len(txt)
        print(f"    → {char_cnt} chars", flush=True)
        if char_cnt < FLOOR:
            print(f"    still blank after OCR", file=sys.stderr, flush=True)
            still_blank += 1
            # leave status=skipped / skip_reason=no-text so we can retry later
            c.execute(
                "UPDATE kbsz_reports SET narrative_text=?, updated_at=? WHERE case_id=?",
                (txt, now(), cid),
            )
            c.commit()
            continue
        # Extract metadata from OCR text
        event_date = parse_date(txt)
        registration = parse_registration(txt)
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        probable_cause = parse_probable_cause(txt)
        lang = detect_lang(txt)
        c.execute(
            """UPDATE kbsz_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?, lang=?,
                 status='parsed', skip_reason=NULL, updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft, location, lang, now(), cid),
        )
        c.commit()
        ocr_ok += 1
        print(f"    date={event_date} reg={registration} lang={lang}", flush=True)
    print(f"[kbsz parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank

# ---- BUILD -------------------------------------------------------------------


def build(c):
    """Write kbsz_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM kbsz_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE kbsz_reports SET status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        # Prefix the authority id: bare KBSZ ids (YYYY-NNN[-N]) collide with other
        # authorities' numbering (hit live: aaiu Ireland shares 2006-002 etc.), and
        # accident_articles is keyed by case_id WITHOUT source — ids must be
        # globally unique across the program.
        cid = "kbsz-" + r["case_id"]
        # site_slug: lowercase prefixed case_id (e.g. "kbsz-2016-0241-4")
        slug = cid.lower()

        # Try to parse operator from narrative if not yet available
        operator = parse_operator(narr)

        c.execute(
            """INSERT OR REPLACE INTO kbsz_accidents
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
                r["lang"] or "en",
                now(),
            ),
        )
        c.execute(
            "UPDATE kbsz_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),  # bare id matches kbsz_reports PK (not kbsz-prefixed)
        )
        c.commit()
        built += 1
    print(f"[kbsz build] built={built}", flush=True)
    return built

# ---- RECHECK -----------------------------------------------------------------

def recheck(c, cl):
    """Re-query CDX for previously not-archived PDFs.

    Cheap: only checks rows with status='skipped' reason='not-archived'.
    If a new snapshot is found, resets status to 'new' so next fetch picks it up.
    """
    rows = c.execute(
        "SELECT case_id, source_url FROM kbsz_reports WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    print(f"[kbsz recheck] checking {len(rows)} not-archived URLs", flush=True)
    newly_available = 0
    for row in rows:
        ts = cdx_best_snapshot(cl, row["source_url"])
        if ts:
            print(f"  [recheck] {row['case_id']} now has snapshot ts={ts}", flush=True)
            c.execute(
                "UPDATE kbsz_reports SET status='new', skip_reason=NULL, updated_at=? WHERE case_id=?",
                (now(), row["case_id"]),
            )
            c.commit()
            newly_available += 1
    print(f"[kbsz recheck] newly_available={newly_available}", flush=True)
    return newly_available

# ---- STATS -------------------------------------------------------------------

def print_stats(c):
    print("\n--- status counts ---")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM kbsz_reports GROUP BY status, skip_reason"):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):20s} {row['n']}")
    cnt = c.execute("SELECT COUNT(*) FROM kbsz_accidents").fetchone()[0]
    print(f"\n--- kbsz_accidents: {cnt} rows ---")
    if cnt:
        row = c.execute(
            "SELECT SUM(lang='en'), SUM(lang='hu'), "
            "MIN(LENGTH(narrative_text)), "
            "MAX(LENGTH(narrative_text)) FROM kbsz_accidents"
        ).fetchone()
        print(f"  lang en={row[0]} hu={row[1]}  narr_len min={row[2]} max={row[3]}")
        null_dates = c.execute("SELECT COUNT(*) FROM kbsz_accidents WHERE event_date IS NULL").fetchone()[0]
        print(f"  event_date NULL: {null_dates}")
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, site_slug, registration, event_date, LENGTH(narrative_text) len "
            "FROM kbsz_accidents ORDER BY case_id LIMIT 8"
        ):
            print(f"    {r['case_id']:20s}  slug={r['site_slug']:20s}  reg={r['registration'] or 'NULL':12s}  date={r['event_date'] or 'NULL'}  narr={r['len']}")
    # not-archived list
    na = c.execute(
        "SELECT case_id FROM kbsz_reports WHERE status='skipped' AND skip_reason='not-archived' ORDER BY case_id"
    ).fetchall()
    if na:
        print(f"\n  not-archived ({len(na)}): {[r['case_id'] for r in na]}")

# ---- MAIN -------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "all"):
        cl = http()
        try:
            discover(c, cl)
        finally:
            cl.close()

    if mode in ("fetch", "all"):
        cl = http()
        try:
            fetch(c, cl)
        finally:
            cl.close()

    if mode in ("parse", "all"):
        parse(c)

    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}", flush=True)

    if mode in ("build", "all", "parse-skipped"):
        build(c)

    if mode == "recheck":
        cl = http()
        try:
            newly = recheck(c, cl)
            print(f"recheck: {newly} newly available")
            if newly:
                print("Run 'fetch' then 'parse' then 'build' to ingest them.")
        finally:
            cl.close()

    print_stats(c)

if __name__ == "__main__":
    main()
