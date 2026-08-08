#!/usr/bin/env python3
"""otsb (Oman — Oman Transport Safety Bureau, MTCIT) aviation-accident ingest.

Two source portals:
  NEW (Odoo CMS): https://mtcit.gov.om/library-3/report-studies-9/reports-79/
    Listing pages: final-report-1254, preliminary-reports-1255,
                   interim-statements-1256, final-reports-before-2019-982
    PDF download: /web/content/{id}?unique=<hash>  (Odoo attachment)

  OLD (ITA/PACA portal): https://prod.mtcit.gov.om/ITAPortal/Pages/Page.aspx?NID=292531&PID=391044
    PDF hrefs: ../Data/SiteImgGallery/{ts}/{name}.pdf
    Resolve against: https://www.mtcit.gov.om/ITAPortal/

Both portals reachable from minipc and hetzner DE without auth.

case_id: 'otsb-' + normalised AIFN ref (aifn-NNN-MM-YYYY) when found in text/title.
         For pre-2019 scans without AIFN: 'otsb-pre2019-rN' (N = report number).
event_date: Date of Occurrence from PDF text or HTML card metadata (not publication date).
report_type: 'final' | 'preliminary' | 'interim'
superseded_by: final case_id for a preliminary/interim of the same AIFN event.

Stages: discover | fetch | parse | parse-skipped | build | all
  parse-skipped: OCR pass on scanned PDFs (OCR_REMOTE=<ocr-host>).
"""

import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile, uuid, urllib.request

NEW_BASE = "https://mtcit.gov.om"
OLD_BASE = "https://www.mtcit.gov.om"
PROD_BASE = "https://prod.mtcit.gov.om"

# New portal listing URLs
NEW_PAGES = {
    "final":      NEW_BASE + "/library-3/report-studies-9/reports-79/final-report-1254",
    "preliminary": NEW_BASE + "/library-3/report-studies-9/reports-79/preliminary-reports-1255",
    "interim":    NEW_BASE + "/library-3/report-studies-9/reports-79/interim-statements-1256",
    "pre2019":    NEW_BASE + "/library-3/report-studies-9/reports-79/final-reports-before-2019-982",
}

# Old portal listing URL
OLD_PAGE = PROD_BASE + "/ITAPortal/Pages/Page.aspx?NID=292531&PID=391044"
OLD_RELATIVE_BASE = OLD_BASE + "/ITAPortal/"

DELAY = 2.0
FLOOR = 300      # minimum chars for usable narrative
HOME = os.path.expanduser("~/otsb-ingest")
DB = os.path.join(HOME, "otsb.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OCR_LANG = "eng"

# SLA/navigation link to skip (content ID always present as non-report)
_SKIP_CONTENT_IDS = {"46367"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS otsb_reports (
  case_id        TEXT PRIMARY KEY,
  aifn_ref       TEXT,              -- normalised AIFN ref e.g. 'aifn-001-07-2022'
  source_url     TEXT,              -- canonical PDF URL on mtcit.gov.om
  content_id     TEXT,              -- Odoo /web/content/{id} when from new portal
  pdf_path       TEXT,
  title          TEXT,              -- button/link text from listing page
  occurrence_date TEXT,             -- Date of Occurrence from page metadata
  publication_date TEXT,
  event_date     TEXT,              -- resolved occurrence date (ISO YYYY-MM-DD)
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  report_type    TEXT,              -- 'final' | 'preliminary' | 'interim'
  superseded_by  TEXT,              -- final case_id for prelim/interim rows
  lang           TEXT DEFAULT 'en',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  portal         TEXT,              -- 'new' | 'old'
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS otsb_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'OM',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  fatalities_total INT,
  phase          TEXT,
  category       TEXT,
  lang           TEXT DEFAULT 'en',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_otsb_status ON otsb_reports(status);
CREATE INDEX IF NOT EXISTS idx_otsb_aifn ON otsb_reports(aifn_ref);
"""

# ---- DB helpers ------------------------------------------------------------

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


# ---- HTTP ------------------------------------------------------------------

def http():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=15.0,
        follow_redirects=True,
    )


def _get(cl, url, retries=3):
    """GET with max-time=15s, exponential backoff on 429/503."""
    delay = DELAY
    for attempt in range(retries):
        try:
            r = cl.get(url, timeout=15.0)
            if r.status_code in (429, 503):
                wait = 30 * (2 ** attempt)
                print("  [throttle] %s -> sleep %ds" % (r.status_code, wait), flush=True)
                time.sleep(wait)
                continue
            time.sleep(delay)
            return r
        except Exception as e:
            print("  [warn] GET %s attempt %d failed: %s" % (url[:60], attempt+1, e), flush=True)
            if attempt < retries - 1:
                time.sleep(15)
    return None


# ---- OCR helpers -----------------------------------------------------------

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


# ---- Text helpers ----------------------------------------------------------

def extract_text(pdf_path):
    """Extract text from PDF. Returns '' if scanned/no-layer."""
    if not pdf_path or not os.path.exists(pdf_path):
        return ""
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=60,
        )
        return r.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


# AIFN normalisation: 'AIFN001.07.2022' -> 'aifn-001-07-2022'
_AIFN_PAT = re.compile(
    r'\b(?:AIFN|IIFN)\s*[-./]?\s*(\d{3})\s*[./\-]?\s*(\d{1,2})\s*[./\-]?\s*(\d{4})\b',
    re.IGNORECASE,
)

def normalise_aifn(text):
    """Extract and normalise first AIFN reference from text.
    Returns e.g. 'aifn-001-07-2022' or None."""
    if not text:
        return None
    m = _AIFN_PAT.search(text)
    if not m:
        return None
    seq, mo, yr = m.group(1), m.group(2).zfill(2), m.group(3)
    return "aifn-%s-%s-%s" % (seq.zfill(3), mo, yr)


def aifn_to_case_id(aifn_norm):
    return "otsb-" + aifn_norm


_DATE_DMY = re.compile(r'\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b')
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

def parse_date(text, occ_date_hint=None):
    """Parse occurrence date from PDF text or HTML hint. Returns ISO YYYY-MM-DD or None."""
    # Prefer explicit hint from HTML (already cleaned DD/MM/YYYY)
    if occ_date_hint:
        m = _DATE_DMY.match(occ_date_hint.strip())
        if m:
            d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31 and 2000 <= yr <= 2030:
                return "%04d-%02d-%02d" % (yr, mo, d)
    # Search PDF text for date patterns:
    # 'Date of Occurrence: DD Month YYYY' / 'Date and Time of Occurrence: ...' /
    # 'Date of the Occurrence:' (old PACA format)
    m = re.search(r'Date(?:\s+and\s+Time)?(?:\s+of\s+the)?\s+Occurrence\s*:?\s*(\d{1,2})[stndrh]{0,2}\s+([A-Za-z]+)\s+(\d{4})', text, re.I)
    if m:
        d, mo_s, yr = int(m.group(1)), m.group(2).lower().strip("."), int(m.group(3))
        mo = _MONTHS_EN.get(mo_s)
        if mo:
            return "%04d-%02d-%02d" % (yr, mo, d)
    # DD/MM/YYYY near occurrence keywords
    m = re.search(r'(?:occurrence|accident|incident|event)[^.]{0,80}?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})', text, re.I)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return "%04d-%02d-%02d" % (yr, mo, d)
    return None


_REG_OM = re.compile(r'\bA4O-[A-Z]{2,4}\b')
_REG_GENERIC = re.compile(
    r'\b(?:A6-[A-Z]{3}|VT-[A-Z]{3,4}|9V-[A-Z]{3}|AP-[A-Z]{3}|HZ-[A-Z]{3,5}|'
    r'SU-[A-Z]{3}|EP-[A-Z]{3}|[A-Z]{1,2}-[A-Z]{3,4}|N\d{3,5}[A-Z]{0,2})\b'
)

def parse_registration(text):
    """Extract primary aircraft registration. Prefers Omani A4O-XXX."""
    m = _REG_OM.search(text)
    if m:
        return m.group()
    m = _REG_GENERIC.search(text)
    if m:
        return m.group()
    return None


def parse_aircraft(text):
    """Extract aircraft type from PDF text."""
    m = re.search(r'(?:Make\s+and\s+Model|Aircraft\s+Type|Aircraft\s+Make)[^\n:]{0,5}:?\s*([^\n]{5,60})', text, re.I)
    if m:
        return m.group(1).strip().split('\n')[0].strip()
    m = re.search(
        r'\b(Boeing\s+\d{3,4}[^\n,;]{0,40}|Airbus\s+A-?\d{3}[^\n,;]{0,40}|'
        r'ATR\s+\d{2}[^\n,;]{0,20}|Embraer\s+[^\n,;]{0,30}|Cessna\s+[^\n,;]{0,30})\b',
        text, re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def parse_operator(text):
    """Extract operator/airline from PDF text."""
    m = re.search(r'Operator\s*:?\s*([^\n]{3,60})', text, re.I)
    if m:
        val = m.group(1).strip()
        if len(val) >= 3:
            return val[:80]
    return None


def parse_location(text):
    """Extract location from PDF text."""
    m = re.search(r'Location\s+of\s+(?:the\s+)?[Oo]ccurrence\s*:?\s*([^\n]{5,80})', text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'Muscat\s+(?:International\s+Airport|FIR|Flight\s+Information)', text, re.I)
    if m:
        return "Muscat, Oman"
    return None


def parse_probable_cause(text):
    """Extract probable cause section from PDF text."""
    m = re.search(
        r'(?:Probable\s+Cause|Contributing\s+Factor|Safety\s+Conclusion)[s]?\s*:?\s*\n+([\s\S]{30,800}?)(?:\n\n|\Z)',
        text, re.I,
    )
    if m:
        cause = m.group(1).strip()
        return cause[:600] if len(cause) > 600 else cause
    return None


def clean_html(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


# ---- DISCOVER: new portal --------------------------------------------------

def _parse_new_listing_page(html, report_type):
    """Parse an Odoo listing page. Returns list of dicts."""
    entries = []
    content_links = list(re.finditer(r'/web/content/(\d+)\?unique=([a-f0-9]+)', html))
    for m in content_links:
        cid = m.group(1)
        if cid in _SKIP_CONTENT_IDS:
            continue
        idx = m.start()
        chunk_before = html[max(0, idx - 900): idx]
        after = html[idx: idx + 400]

        # Occurrence date from card metadata
        occ_m = re.search(r'Date\s+of\s+Occurrence\s*:?[^>]*>\s*<strong>([^<]+)</strong>', chunk_before, re.DOTALL)
        occ_date = occ_m.group(1).strip() if occ_m else None

        # Publication date
        pub_m = re.search(r'Date\s+of\s+Publication\s*:?[^>]*>\s*<strong>([^<]+)</strong>', chunk_before, re.DOTALL)
        pub_date = pub_m.group(1).strip() if pub_m else None

        # Button text / title: idx points at '/web/content/...', take context including href=
        after_with_href = html[max(0, idx - 7): idx + 400]
        btn_m = re.search(r'href="[^"]*"(?:[^>]*)>([^<]{5,300})', after_with_href)
        title = clean_html(btn_m.group(1)).strip() if btn_m else None

        entries.append({
            "content_id": cid,
            "source_url": "https://mtcit.gov.om/web/content/%s" % cid,
            "title": title,
            "occurrence_date": occ_date,
            "publication_date": pub_date,
            "report_type": report_type,
            "portal": "new",
        })
    return entries


def _parse_old_portal_page(html):
    """Parse old PACA portal listing page. Returns list of dicts."""
    entries = []
    old_rel_base = "https://www.mtcit.gov.om/ITAPortal/"
    for m in re.finditer(r'href="((?:\.\.\/Data\/SiteImgGallery|https?://[^"]*Data/SiteImgGallery)[^"]*\.pdf)"', html, re.I):
        raw = m.group(1)
        if raw.startswith("../"):
            url = old_rel_base + raw[3:]
        else:
            url = raw
        fn = urllib.parse.unquote(url.split("/")[-1])
        # Classify by filename
        if re.search(r'\bpreliminary\b', fn, re.I):
            rtype = "preliminary"
        elif re.search(r'\binterim\b', fn, re.I):
            rtype = "interim"
        else:
            rtype = "final"
        entries.append({
            "content_id": None,
            "source_url": url,
            "title": fn,
            "occurrence_date": None,
            "publication_date": None,
            "report_type": rtype,
            "portal": "old",
        })
    return entries


def _make_case_id(entry):
    """Derive case_id from entry dict."""
    aifn = None
    if entry.get("title"):
        aifn = normalise_aifn(entry["title"])
    if aifn:
        return aifn_to_case_id(aifn)
    # pre-2019 scanned 'Final Report N.pdf' -> otsb-pre2019-rN
    fn = urllib.parse.unquote((entry.get("source_url") or "").split("/")[-1])
    m = re.search(r'(?:Final\s+Report|FINAL\s+REPORT)\s+(\d+)', fn, re.I)
    if m:
        return "otsb-pre2019-r%s" % m.group(1)
    # fallback: content_id or filename slug
    if entry.get("content_id"):
        return "otsb-content-%s" % entry["content_id"]
    slug = re.sub(r'[^a-z0-9]+', '-', fn.lower().replace('.pdf', ''))[:60].strip('-')
    return "otsb-" + slug


def discover(c):
    """Enumerate both portals and insert new rows into otsb_reports."""
    cl = http()
    all_entries = []

    # --- New portal ---
    for rtype, url in NEW_PAGES.items():
        print("[otsb discover] fetching new portal: %s" % url, flush=True)
        r = _get(cl, url)
        if not r or r.status_code != 200:
            print("  [warn] HTTP %s for %s" % (r.status_code if r else 'err', url), flush=True)
            continue
        entries = _parse_new_listing_page(r.text, rtype if rtype != "pre2019" else "final")
        print("  found %d entries on %s page" % (len(entries), rtype), flush=True)
        all_entries.extend(entries)

    # --- Old portal ---
    print("[otsb discover] fetching old portal: %s" % OLD_PAGE, flush=True)
    r = _get(cl, OLD_PAGE)
    if r and r.status_code == 200:
        old_entries = _parse_old_portal_page(r.text)
        print("  found %d entries on old portal" % len(old_entries), flush=True)
        all_entries.extend(old_entries)
    else:
        print("  [warn] old portal unavailable", flush=True)

    # Insert new rows
    inserted = 0
    skipped_dup = 0
    for entry in all_entries:
        case_id = _make_case_id(entry)
        existing = c.execute(
            "SELECT case_id FROM otsb_reports WHERE case_id=?", (case_id,)
        ).fetchone()
        if existing:
            skipped_dup += 1
            continue
        # Also skip if same source_url already stored under different case_id
        if entry.get("source_url"):
            dup = c.execute(
                "SELECT case_id FROM otsb_reports WHERE source_url=?",
                (entry["source_url"],)
            ).fetchone()
            if dup:
                skipped_dup += 1
                continue
        aifn = normalise_aifn(entry.get("title") or "")
        c.execute(
            """INSERT INTO otsb_reports
               (case_id, aifn_ref, source_url, content_id, title,
                occurrence_date, publication_date, report_type, portal,
                status, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (
                case_id,
                aifn,
                entry["source_url"],
                entry.get("content_id"),
                entry.get("title"),
                entry.get("occurrence_date"),
                entry.get("publication_date"),
                entry["report_type"],
                entry["portal"],
                now(), now(),
            ),
        )
        c.commit()
        inserted += 1
        print("  [+] %s (%s, %s)" % (case_id, entry["report_type"], entry["portal"]), flush=True)

    print("[otsb discover] inserted=%d skipped_dup=%d" % (inserted, skipped_dup), flush=True)


# ---- FETCH -----------------------------------------------------------------

def fetch(c):
    """Download PDFs for all 'new' status rows."""
    rows = c.execute(
        "SELECT case_id, source_url, content_id FROM otsb_reports WHERE status='new'"
    ).fetchall()
    print("[otsb fetch] %d rows to fetch" % len(rows), flush=True)
    cl = http()
    ok = fail = 0
    for row in rows:
        case_id = row["case_id"]
        url = row["source_url"]
        safe = re.sub(r'[^a-z0-9.\-]', '_', case_id.lower())[:80]
        pdf_path = os.path.join(PDFDIR, safe + ".pdf")
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1000:
            c.execute(
                "UPDATE otsb_reports SET status='fetched', pdf_path=?, updated_at=? WHERE case_id=?",
                (pdf_path, now(), case_id),
            )
            c.commit()
            ok += 1
            continue
        print("  fetching %s <- %s" % (case_id, url[:80]), flush=True)
        r = _get(cl, url)
        if not r or r.status_code != 200:
            print("  [fail] HTTP %s" % (r.status_code if r else 'err'), flush=True)
            c.execute(
                "UPDATE otsb_reports SET status='skipped', skip_reason='fetch-fail', updated_at=? WHERE case_id=?",
                (now(), case_id),
            )
            c.commit()
            fail += 1
            continue
        # Verify it's a PDF
        if b'%PDF' not in r.content[:10]:
            print("  [fail] not a PDF (content-type=%s)" % r.headers.get('content-type', '?'), flush=True)
            c.execute(
                "UPDATE otsb_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                (now(), case_id),
            )
            c.commit()
            fail += 1
            continue
        os.makedirs(PDFDIR, exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(r.content)
        c.execute(
            "UPDATE otsb_reports SET status='fetched', pdf_path=?, updated_at=? WHERE case_id=?",
            (pdf_path, now(), case_id),
        )
        c.commit()
        ok += 1
        print("  [ok] %d bytes" % len(r.content), flush=True)
    print("[otsb fetch] ok=%d fail=%d" % (ok, fail), flush=True)


# ---- PARSE -----------------------------------------------------------------

def _resolve_supersession(c):
    """Set superseded_by on prelim/interim rows when a final with same aifn exists."""
    rows = c.execute(
        "SELECT case_id, aifn_ref, report_type FROM otsb_reports WHERE aifn_ref IS NOT NULL"
    ).fetchall()
    # Group by aifn_ref
    by_aifn = {}
    for r in rows:
        by_aifn.setdefault(r["aifn_ref"], []).append(r)
    updated = 0
    for aifn, group in by_aifn.items():
        finals = [r for r in group if r["report_type"] == "final"]
        non_finals = [r for r in group if r["report_type"] != "final"]
        if not finals or not non_finals:
            continue
        final_id = finals[0]["case_id"]
        for nf in non_finals:
            c.execute(
                "UPDATE otsb_reports SET superseded_by=?, updated_at=? WHERE case_id=? AND superseded_by IS NULL",
                (final_id, now(), nf["case_id"]),
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                updated += 1
    c.commit()
    print("[otsb parse] supersession links set: %d" % updated, flush=True)


def parse(c):
    """Extract metadata from downloaded PDFs for 'fetched' rows."""
    rows = c.execute(
        "SELECT case_id, pdf_path, occurrence_date, publication_date, report_type, aifn_ref, title "
        "FROM otsb_reports WHERE status='fetched'"
    ).fetchall()
    print("[otsb parse] %d rows to parse" % len(rows), flush=True)
    parsed = skipped = 0
    for row in rows:
        case_id = row["case_id"]
        pdf_path = row["pdf_path"]
        text = extract_text(pdf_path)
        if len(text) < FLOOR:
            # Scanned PDF — defer to parse-skipped (OCR pass)
            c.execute(
                "UPDATE otsb_reports SET status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (now(), case_id),
            )
            c.commit()
            skipped += 1
            print("  [scan] %s needs OCR (%d chars)" % (case_id, len(text)), flush=True)
            continue
        # Extract metadata
        event_date = parse_date(text, row["occurrence_date"])
        aircraft = parse_aircraft(text)
        registration = parse_registration(text)
        operator = parse_operator(text)
        location = parse_location(text)
        probable_cause = parse_probable_cause(text)
        # Narrative is full text (cap at 20K chars)
        narrative = text[:20000]
        # Prefer aifn_ref from PDF text over title (title may have typos like 2005→2025)
        aifn_from_pdf = normalise_aifn(text)
        if aifn_from_pdf and aifn_from_pdf != row["aifn_ref"]:
            print("  [aifn-fix] %s: title_aifn=%s pdf_aifn=%s" % (case_id, row["aifn_ref"], aifn_from_pdf), flush=True)
        effective_aifn = aifn_from_pdf or row["aifn_ref"]

        c.execute(
            """UPDATE otsb_reports SET
               status='parsed', aifn_ref=?, event_date=?, aircraft=?, registration=?,
               operator=?, location=?, narrative_text=?, probable_cause=?,
               updated_at=?
               WHERE case_id=?""",
            (effective_aifn, event_date, aircraft, registration, operator, location,
             narrative, probable_cause, now(), case_id),
        )
        c.commit()
        parsed += 1
        print("  [ok] %s date=%s acft=%s" % (case_id, event_date, aircraft or '?'), flush=True)

    _resolve_supersession(c)
    print("[otsb parse] parsed=%d skipped(no-text)=%d" % (parsed, skipped), flush=True)


# ---- PARSE-SKIPPED (OCR pass) ----------------------------------------------

def parse_skipped(c):
    """OCR pass for scanned PDFs. Requires OCR_REMOTE env."""
    rows = c.execute(
        "SELECT case_id, pdf_path, occurrence_date, report_type "
        "FROM otsb_reports WHERE status='skipped' AND skip_reason='no-text'"
    ).fetchall()
    print("[otsb parse-skipped] %d rows to OCR" % len(rows), flush=True)
    ocr_ok = still_blank = 0
    for row in rows:
        case_id = row["case_id"]
        pdf_path = row["pdf_path"]
        print("  [ocr] %s" % case_id, flush=True)
        text = ocr_extract(pdf_path)
        if len(text) < FLOOR:
            still_blank += 1
            print("  [ocr-fail] still blank (%d chars)" % len(text), flush=True)
            c.execute(
                "UPDATE otsb_reports SET skip_reason='ocr-blank', updated_at=? WHERE case_id=?",
                (now(), case_id),
            )
            c.commit()
            continue
        event_date = parse_date(text, row["occurrence_date"])
        aircraft = parse_aircraft(text)
        registration = parse_registration(text)
        operator = parse_operator(text)
        location = parse_location(text)
        probable_cause = parse_probable_cause(text)
        narrative = text[:20000]
        aifn_from_pdf = normalise_aifn(text)
        c.execute(
            """UPDATE otsb_reports SET
               status='parsed', skip_reason=NULL, aifn_ref=?,
               event_date=?, aircraft=?, registration=?,
               operator=?, location=?, narrative_text=?, probable_cause=?,
               updated_at=?
               WHERE case_id=?""",
            (aifn_from_pdf, event_date, aircraft, registration, operator, location,
             narrative, probable_cause, now(), case_id),
        )
        c.commit()
        ocr_ok += 1
        print("  [ocr-ok] %s date=%s %d chars" % (case_id, event_date, len(text)), flush=True)
    _resolve_supersession(c)
    print("[otsb parse-skipped] ocr_ok=%d still_blank=%d" % (ocr_ok, still_blank), flush=True)
    return ocr_ok, still_blank


# ---- BUILD -----------------------------------------------------------------

def build(c):
    """Write otsb_accidents from parsed rows.
    Superseded rows (prelim/interim that have a final) are skipped.
    For same aifn_ref with multiple portals, prefer new portal final report."""
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, operator, location,
                  narrative_text, probable_cause, source_url, report_type,
                  superseded_by, aifn_ref, portal
           FROM otsb_reports WHERE status='parsed'"""
    ).fetchall()

    # Dedup: for same aifn_ref keep best row (new portal final > old portal final > others)
    # Build a priority map: aifn_ref -> best_case_id
    aifn_best = {}  # aifn_ref -> row with highest priority
    for r in rows:
        aifn = r["aifn_ref"]
        if not aifn:
            continue
        existing = aifn_best.get(aifn)
        if existing is None:
            aifn_best[aifn] = r
        else:
            # Priority: new+final > old+final > new+prelim > old+prelim > interim
            def priority(row):
                rtype_score = {"final": 3, "preliminary": 1, "interim": 0}.get(row["report_type"] or "", 0)
                portal_score = 2 if row["portal"] == "new" else 0
                return rtype_score + portal_score
            if priority(r) > priority(existing):
                aifn_best[aifn] = r
    # Build set of preferred case_ids per aifn_ref
    preferred_ids = {v["case_id"] for v in aifn_best.values()}

    built = superseded_skipped = no_text = dedup_skipped = 0
    for r in rows:
        # Skip superseded prelim/interim rows
        if r["superseded_by"]:
            superseded_skipped += 1
            continue
        # Skip duplicates (same aifn_ref, not the preferred row)
        if r["aifn_ref"] and r["case_id"] not in preferred_ids:
            dedup_skipped += 1
            prefer = aifn_best.get(r["aifn_ref"])
            prefer_id = prefer["case_id"] if prefer else "?"
            print("  [dedup-skip] %s (prefer %s)" % (r["case_id"], prefer_id), flush=True)
            continue
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            no_text += 1
            continue
        cid = r["case_id"]
        slug = cid.lower()
        c.execute(
            """INSERT OR REPLACE INTO otsb_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, fatalities_total, phase, category, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'OM', ?, ?, ?, ?, ?, NULL, NULL, NULL, 'en', ?)""",
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
                r["report_type"] or "final",
                slug,
                now(),
            ),
        )
        c.execute(
            "UPDATE otsb_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), cid),
        )
        c.commit()
        built += 1
    print("[otsb build] built=%d superseded_skipped=%d no_text=%d dedup_skipped=%d" % (built, superseded_skipped, no_text, dedup_skipped), flush=True)
    return built


# ---- STATS -----------------------------------------------------------------

def print_stats(c):
    print("\n--- otsb_reports status ---", flush=True)
    for row in c.execute(
        "SELECT status, skip_reason, report_type, count(*) n "
        "FROM otsb_reports GROUP BY status, skip_reason, report_type ORDER BY n DESC"
    ):
        print("  %-10s %-20s %-15s %d" % (
            row["status"], row["skip_reason"] or "", row["report_type"] or "", row["n"]
        ), flush=True)

    cnt = c.execute("SELECT COUNT(*) FROM otsb_accidents").fetchone()[0]
    print("\n--- otsb_accidents: %d rows ---" % cnt, flush=True)
    if cnt:
        row = c.execute(
            "SELECT SUM(CASE WHEN event_date IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN LENGTH(narrative_text) < 300 THEN 1 ELSE 0 END), "
            "MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM otsb_accidents"
        ).fetchone()
        print("  event_date NULL: %s | narr<300: %s | narr_len min=%s max=%s" % (
            row[0], row[1], row[2], row[3]
        ), flush=True)
        # Dup check
        dups = c.execute(
            "SELECT case_id, COUNT(*) n FROM otsb_accidents GROUP BY case_id HAVING n > 1"
        ).fetchall()
        print("  dups=%d" % len(dups), flush=True)
        print("\n  report_type distribution:", flush=True)
        for r2 in c.execute("SELECT report_type, count(*) FROM otsb_accidents GROUP BY report_type"):
            print("    %-15s %d" % (r2[0] or 'NULL', r2[1]), flush=True)
        print("\n  sample rows:", flush=True)
        for r2 in c.execute(
            "SELECT case_id, registration, event_date, LENGTH(narrative_text) len "
            "FROM otsb_accidents ORDER BY event_date"
        ):
            print("    %-50s reg=%-12s date=%s narr=%d" % (
                r2["case_id"][:50], r2["registration"] or "NULL",
                r2["event_date"] or "NULL", r2["len"]
            ), flush=True)


# ---- MAIN ------------------------------------------------------------------

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
        print("parse-skipped: ocr_ok=%d still_blank=%d" % (ok, blank), flush=True)
        if ok:
            build(c)

    if mode in ("build", "all"):
        build(c)

    print_stats(c)


if __name__ == "__main__":
    main()
