#!/usr/bin/env python3
"""AKISA (Albania — Autoriteti Kombëtar i Investigimit për Sigurinë e Operimit
në Aviacionin Civil) aviation accident ingest.

Source: akisa.gov.al — WordPress (Divi theme), bilingual EN/AL (sq).
WP JSON API is WAF-blocked; HTML crawl is used instead.

Discovery strategy:
  1. Walk WP post-sitemap.xml to get every post URL
  2. Fetch each post page; extract PDF links from href attributes
     (both absolute https://akisa.gov.al/wp-content/... and relative /wp-content/...)
  3. Skip non-investigation posts (news, annual-report, etc.)
  4. AL versions (lang='sq') are the canonical source; EN counterparts
     are tracked for deduplication (same physical event = same case_id).

Known reports (as of 2026-06-11):
  - Blue Panorama B737 Tirana landing (date from PDF)
    AL slug: blue-panorama-raport-final
  - I-GRMP Cessna 172 Divjaka crash 2014-05-10
    AL slug: aksidenti-ajroror-ne-divjake-al
    EN slug: en/aircraft-accident-in-divjaka
  - Ishem River helicopter accident (date from PDF)
    AL slug: aksidenti-ajror-lumi-ishem-al
  - Guarda di Finanza incident (date from PDF)
    EN slug: en/final-report-incident-guarda-di-finanza

case_id = 'akisa-' + registration.lower() OR 'akisa-' + pdf_stem_slug
event_date: parsed from OCR text (Albanian "10 maj 2014" or numeric DD.MM.YYYY)
lang: 'sq' for AL PDFs, 'en' for EN-only PDFs
All PDFs are scanned → OCR_REMOTE required (hetzner, sqi + eng tesseract).

Stages: discover | fetch | parse | build | all
"""
import sys, os, re, time, sqlite3, shlex, subprocess, tempfile

BASE = "https://akisa.gov.al"
SITEMAP = BASE + "/post-sitemap.xml"
DELAY = 2.0
FLOOR = 300          # absolute build floor (chars; per spec)
MIN_NARRATIVE = 600  # preferred tier
HOME = os.path.expanduser("~/akisa-ingest")
DB = os.path.join(HOME, "akisa.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
OCR_LANG_AL = "sqi"
OCR_LANG_EN = "eng"
OCR_LANG_BOTH = "sqi+eng"

SCHEMA = """
CREATE TABLE IF NOT EXISTS akisa_reports (
  case_id     TEXT PRIMARY KEY,
  pdf_url     TEXT,
  pdf_path    TEXT,
  page_url    TEXT,
  page_lang   TEXT,   -- 'sq' or 'en'
  title       TEXT,
  report_type TEXT DEFAULT 'Final report',
  registration TEXT,
  event_date  TEXT,
  location    TEXT,
  narrative_text  TEXT,
  source_tier TEXT,
  lang        TEXT DEFAULT 'sq',
  status      TEXT DEFAULT 'new',
  discovered_at INT,
  updated_at  INT
);
CREATE TABLE IF NOT EXISTS akisa_accidents (
  case_id         TEXT PRIMARY KEY,
  event_date      TEXT,
  aircraft        TEXT,
  registration    TEXT,
  operator        TEXT,
  location        TEXT,
  country         TEXT DEFAULT 'AL',
  narrative_text  TEXT,
  probable_cause  TEXT,
  source_url      TEXT,
  report_type     TEXT,
  site_slug       TEXT,
  lang            TEXT DEFAULT 'sq',
  fatalities_total INT,
  phase           TEXT,
  category        TEXT,
  built_at        INT
);
CREATE INDEX IF NOT EXISTS idx_akisa_status ON akisa_reports(status);
"""

# Posts whose slugs contain these tokens are NOT investigation reports
_SKIP_TOKENS = [
    "lajme", "news", "workshop", "newsletter", "statistik", "annual-report",
    "raport-vjetor", "studime", "agreement", "marreveshje", "gezuar", "happy-new",
    "mbledhje", "kryeministr", "prime-minister", "aeroporti", "airport",
    "takim", "nderinstitucional", "canada", "airbus", "japan-airlines",
    "njoftim-zyrtar",  # official announcements (no PDF report)
    "akisa-dhe", "akisa-merr", "aac-dhe", "caa-and",
    "vendim", "on-cloud-nine", "vlora", "azzano",
]
# Categories that ARE investigation reports (used as positive filter on page)
_REPORT_CATEGORIES = {"final-report", "raport-final"}

def now():
    return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def http_get(url, timeout=15, retries=2):
    """Simple httpx GET with retries."""
    import httpx
    headers = {"User-Agent": UA}
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, headers=headers, timeout=timeout,
                          follow_redirects=True)
            return r
        except Exception as e:
            if attempt == retries:
                raise
            print(f"[akisa http] retry {attempt+1} for {url}: {e}", file=sys.stderr)
            time.sleep(2)

# ── OCR (copied from iacm-ingest pattern) ────────────────────────────────────

def _ocr_remote(pdf_path, lang, host):
    import uuid
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print(f"[akisa ocr] scp failed rc={cp.returncode}", file=sys.stderr, flush=True)
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[akisa ocr] remote exception: {e}", file=sys.stderr, flush=True)
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""

def ocr_extract(pdf_path, lang=OCR_LANG_BOTH):
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    # Local fallback
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

def extract_text(path):
    """Try pdftotext first; returns '' for scanned PDFs."""
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

# ── Slug / ID helpers ─────────────────────────────────────────────────────────

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

def _case_id_from_pdf_url(url):
    """Stable case_id from PDF filename; prefix 'akisa-'."""
    fn = url.rstrip("/").split("/")[-1]
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I)
    fn = re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-").lower()
    return ("akisa-" + fn)[:60] or "akisa-unknown"

# ── Registration detection ─────────────────────────────────────────────────────

_REG_RE = re.compile(
    r"\b("
    r"ZA-[A-Z]{3}"        # Albanian civil regs
    r"|[A-Z]{1,2}-[A-Z]{3,4}"  # generic ICAO-style
    r"|I-[A-Z]{4}"        # Italian
    r")\b"
)

def reg_from(text):
    m = _REG_RE.search(text or "")
    return m.group(1).upper() if m else None

# ── Date parsing (Albanian + numeric) ─────────────────────────────────────────

_AL_MONTHS = {
    "janar": 1, "shkurt": 2, "mars": 3, "prill": 4,
    "maj": 5, "qershor": 6, "korrik": 7, "gusht": 8,
    "shtator": 9, "tetor": 10, "nentor": 11, "dhjetor": 12,
    # common variants
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_AL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_AL_MONTHS.keys()) + r")\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_NUM_DATE_RE = re.compile(r"\b([0-3]?\d)[./-]([01]?\d)[./-]((?:19|20)\d{2})\b")

def parse_date(txt):
    """Extract event OCCURRENCE date from OCR text.

    Priority:
    1. Explicit occurrence patterns in section headers:
       "Aksidenti ... dt. DD.MM.YYYY" / "Incident ... in DD.MM.YYYY"
    2. Albanian/English written month in first 2000 chars
    3. First numeric DD.MM.YYYY in first 2000 chars

    Multi-incident reports: use the FIRST section-header occurrence date.
    """
    if not txt:
        return None

    # Priority 1: section header pattern (most reliable — occurrence date)
    sec_patterns = [
        r"[Ss]eksioni\s*1[:\s]+Aksidenti[^\.]*(?:dt|date)\.\s*(\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2})",
        r"[Ss]ection\s*1[:\s]+[A-Za-z ]+in\s+(\d{1,2}[./]\d{2}[./](?:19|20)\d{2})",
        r"(?:aksidentin|ngjarjes|incident)[^\n]{0,60}(\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2})",
    ]
    for pat in sec_patterns:
        m = re.search(pat, txt[:3000], re.I)
        if m:
            ds = m.group(1)
            nm = _NUM_DATE_RE.search(ds)
            if nm:
                d, mo, y = int(nm.group(1)), int(nm.group(2)), int(nm.group(3))
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"

    # Priority 2: Albanian/English written month in first 2000 chars
    m = _AL_DATE_RE.search(txt[:2000])
    if m:
        d = int(m.group(1))
        month_str = m.group(2).lower()
        y = int(m.group(3))
        mo = _AL_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Priority 3: numeric date DD.MM.YYYY / DD/MM/YYYY in first 2000 chars
    m = _NUM_DATE_RE.search(txt[:2000])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

# ── Discover ──────────────────────────────────────────────────────────────────

def _is_report_page(url):
    """Heuristic: is this post URL an investigation report page?"""
    path = url.lower().rstrip("/")
    # Must be a post (has slug)
    for tok in _SKIP_TOKENS:
        if tok in path:
            return False
    # Positive signals
    for pos in ("raport-final", "final-report", "aksidenti", "accident",
                "incident", "helicopter", "helikopter", "divjak", "panorama",
                "guarda", "ishem"):
        if pos in path:
            return True
    return False

def _pdf_links_from_html(html, page_url):
    """Extract PDF URLs from HTML, normalising relative to absolute."""
    links = re.findall(r'(?:href|src)="([^"]*)"', html)
    pdfs = []
    for lnk in links:
        if not re.search(r'\.pdf', lnk, re.I):
            continue
        if lnk.startswith("//"):
            lnk = "https:" + lnk
        elif lnk.startswith("/"):
            lnk = BASE + lnk
        elif not lnk.startswith("http"):
            continue
        # Only PDFs on akisa.gov.al
        if "akisa.gov.al" not in lnk:
            continue
        pdfs.append(lnk)
    return list(dict.fromkeys(pdfs))  # deduplicate preserving order

def _page_lang(url):
    return "en" if "/en/" in url else "sq"

def discover(c):
    """Walk sitemap + extract PDF links from each report page."""
    print("[akisa discover] fetching sitemap ...", flush=True)
    r = http_get(SITEMAP)
    urls = re.findall(r'<loc>(https://akisa\.gov\.al/[^<]+)</loc>', r.text)
    print(f"[akisa discover] sitemap: {len(urls)} URLs", flush=True)

    # Filter to plausible report pages
    report_urls = [u for u in urls if _is_report_page(u)]
    print(f"[akisa discover] candidate report pages: {len(report_urls)}", flush=True)

    inserted = 0
    seen_cases = set(r["case_id"] for r in c.execute("SELECT case_id FROM akisa_reports"))

    for page_url in report_urls:
        print(f"[akisa discover] {page_url}", flush=True)
        try:
            r = http_get(page_url)
        except Exception as e:
            print(f"[akisa discover] SKIP {page_url}: {e}", file=sys.stderr)
            time.sleep(DELAY)
            continue

        pdfs = _pdf_links_from_html(r.text, page_url)
        if not pdfs:
            print(f"[akisa discover]   no PDFs found", flush=True)
            time.sleep(DELAY)
            continue

        # Extract page title for reference
        title_m = re.search(r"<title>([^<]+)</title>", r.text)
        page_title = title_m.group(1).replace(" - AKISA", "").strip() if title_m else ""
        page_lang = _page_lang(page_url)

        for pdf_url in pdfs:
            case_id = _case_id_from_pdf_url(pdf_url)
            if case_id in seen_cases:
                print(f"[akisa discover]   dup skip {case_id}", flush=True)
                continue
            seen_cases.add(case_id)

            # Detect lang from filename
            pdf_fn = pdf_url.split("/")[-1].lower()
            if "-en" in pdf_fn or "-en-" in pdf_fn or pdf_fn.endswith("-en.pdf"):
                lang = "en"
            else:
                lang = page_lang  # default to page language

            # Registration from PDF filename
            reg = reg_from(pdf_url)

            c.execute(
                "INSERT OR IGNORE INTO akisa_reports "
                "(case_id,pdf_url,page_url,page_lang,title,registration,lang,status,discovered_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (case_id, pdf_url, page_url, page_lang, page_title, reg, lang, "new", now(), now()),
            )
            c.commit()
            inserted += 1
            print(f"[akisa discover]   + {case_id} ({lang}) <- {pdf_url.split('/')[-1]}", flush=True)

        time.sleep(DELAY)

    return inserted

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(c):
    rows = c.execute(
        "SELECT case_id, pdf_url FROM akisa_reports WHERE status='new'"
    ).fetchall()
    print(f"[akisa fetch] {len(rows)} PDFs to download", flush=True)
    done = 0
    for row in rows:
        pdf_url = row["pdf_url"]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", row["case_id"]) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)
        print(f"[akisa fetch] {row['case_id']} ...", flush=True)
        try:
            r = http_get(pdf_url)
            if r.status_code != 200:
                print(f"[akisa fetch] HTTP {r.status_code} for {pdf_url}", file=sys.stderr)
                continue
            with open(dest, "wb") as fh:
                fh.write(r.content)
            c.execute(
                "UPDATE akisa_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), row["case_id"]),
            )
            c.commit()
            done += 1
            print(f"[akisa fetch]   saved {len(r.content)//1024}KB -> {os.path.basename(dest)}", flush=True)
        except Exception as e:
            print(f"[akisa fetch] ERROR {row['case_id']}: {e}", file=sys.stderr)
        time.sleep(DELAY)
    return done

# ── Parse (pdftotext + OCR) ───────────────────────────────────────────────────

def parse(c):
    rows = c.execute(
        "SELECT * FROM akisa_reports WHERE status='fetched'"
    ).fetchall()
    print(f"[akisa parse] {len(rows)} rows to parse", flush=True)
    done = 0
    for row in rows:
        pdf_path = row["pdf_path"]
        print(f"[akisa parse] {row['case_id']} ...", flush=True)

        # Try pdftotext first
        txt = extract_text(pdf_path)
        if len(txt) >= FLOOR:
            tier = "pdf"
        else:
            # Scanned; use OCR
            ocr_lang = OCR_LANG_EN if row["lang"] == "en" else OCR_LANG_BOTH
            print(f"[akisa parse]   scanned, OCR lang={ocr_lang} ...", flush=True)
            txt = ocr_extract(pdf_path, ocr_lang)
            tier = "ocr" if len(txt) >= FLOOR else "scanned"

        # Extract metadata from text
        event_date = parse_date(txt) or row["event_date"]
        reg = row["registration"] or reg_from(txt)

        # Try to extract location from first ~800 chars
        location = None
        loc_m = re.search(
            r'(?:location|vend(?:ndodhjia|i|e)|aerodrom|airport|airfield)[:\s]+([^\n,\.]{3,60})',
            txt[:800], re.I,
        )
        if loc_m:
            location = loc_m.group(1).strip()

        c.execute(
            "UPDATE akisa_reports SET narrative_text=?, source_tier=?, registration=COALESCE(?,registration), "
            "event_date=COALESCE(?,event_date), location=COALESCE(?,location), "
            "status='parsed', updated_at=? WHERE case_id=?",
            (txt, tier, reg, event_date, location, now(), row["case_id"]),
        )
        c.commit()
        done += 1
        print(f"[akisa parse]   tier={tier} text_len={len(txt)} date={event_date}", flush=True)
    return done

# ── Build ─────────────────────────────────────────────────────────────────────

def build(c):
    rows = c.execute(
        "SELECT * FROM akisa_reports WHERE status='parsed'"
    ).fetchall()
    print(f"[akisa build] {len(rows)} rows to build", flush=True)
    built = 0
    for row in rows:
        narr = row["narrative_text"] or ""
        if len(narr) < FLOOR:
            print(f"[akisa build] SKIP {row['case_id']}: text_len={len(narr)} < {FLOOR}", flush=True)
            c.execute("UPDATE akisa_reports SET status='skipped', updated_at=? WHERE case_id=?",
                      (now(), row["case_id"]))
            c.commit()
            continue

        # Infer aircraft from common patterns in the text
        aircraft = None
        ac_m = re.search(
            r'\b(Boeing\s+7\d\d[A-Z-]*|Airbus\s+A\d{3}[A-Z-]*|Cessna\s+\w+|'
            r'Piper\s+\w+|Beechcraft\s+\w+|Tecnam\s+\w+|'
            r'[A-Z][a-z]+\s+[A-Z]{1,2}[\s-]\d{2,4}[A-Z-]*)',
            narr[:1000],
        )
        if ac_m:
            aircraft = ac_m.group(1).strip()

        c.execute(
            "INSERT OR REPLACE INTO akisa_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            "narrative_text,probable_cause,source_url,report_type,site_slug,lang,"
            "fatalities_total,phase,category,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["event_date"],
                aircraft,
                row["registration"],
                None,   # operator: extracted by P2
                row["location"],
                "AL",
                narr,
                None,   # probable_cause: extracted by P2
                row["pdf_url"],
                row["report_type"] or "Final report",
                site_slug(row["case_id"]),
                row["lang"],
                None,   # fatalities_total: extracted by P2
                None,   # phase: extracted by P2
                None,   # category: extracted by P2
                now(),
            ),
        )
        c.execute("UPDATE akisa_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), row["case_id"]))
        c.commit()
        built += 1
        print(f"[akisa build] + {row['case_id']} date={row['event_date']} lang={row['lang']}", flush=True)
    return built

# ── Verify ────────────────────────────────────────────────────────────────────

def verify(c):
    print("\n=== VERIFICATION ===", flush=True)
    total = c.execute("SELECT COUNT(*) FROM akisa_accidents").fetchone()[0]
    print(f"  total rows: {total}", flush=True)
    floor_ok = c.execute(
        "SELECT COUNT(*) FROM akisa_accidents WHERE LENGTH(narrative_text) >= ?", (FLOOR,)
    ).fetchone()[0]
    print(f"  >= {FLOOR} chars: {floor_ok}", flush=True)
    dated = c.execute(
        "SELECT COUNT(*) FROM akisa_accidents WHERE event_date IS NOT NULL AND event_date != ''"
    ).fetchone()[0]
    print(f"  dated: {dated} ({100*dated//max(total,1)}%)", flush=True)
    dups = c.execute(
        "SELECT case_id, COUNT(*) n FROM akisa_accidents GROUP BY case_id HAVING n > 1"
    ).fetchall()
    print(f"  duplicates: {len(dups)}", flush=True)
    langs = c.execute(
        "SELECT lang, COUNT(*) FROM akisa_accidents GROUP BY lang"
    ).fetchall()
    print(f"  lang mix: {dict(langs)}", flush=True)
    print("\n  rows by status:", flush=True)
    for row in c.execute("SELECT status, COUNT(*) FROM akisa_reports GROUP BY status"):
        print(f"    {row[0]}: {row[1]}", flush=True)
    print("\n  accidents:", flush=True)
    for row in c.execute("SELECT case_id, event_date, lang, LENGTH(narrative_text) FROM akisa_accidents ORDER BY event_date"):
        print(f"    {row[0]:60s} {row[1]} lang={row[2]} chars={row[3]}", flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "all"):
        n = discover(c)
        print(f"[akisa] discovered: {n} new reports", flush=True)

    if mode in ("fetch", "all"):
        n = fetch(c)
        print(f"[akisa] fetched: {n} PDFs", flush=True)

    if mode in ("parse", "all"):
        n = parse(c)
        print(f"[akisa] parsed: {n} rows", flush=True)

    if mode in ("build", "all"):
        n = build(c)
        print(f"[akisa] built: {n} accidents", flush=True)

    verify(c)


if __name__ == "__main__":
    main()
