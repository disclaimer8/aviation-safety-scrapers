#!/usr/bin/env python3
"""dgaccr (Costa Rica DGAC, country CR, lang 'es') aviation-accident ingest.

Source: sub.dgac.go.cr — recovered ENTIRELY from the Wayback Machine because
the live site is Cloudflare-blocked from non-CR IPs.
DO NOT probe sub.dgac.go.cr directly.

Reports found on web.archive.org:
  Final reports (INFORME-FINAL-APROBADO-* / Informe-Final-*):
    ~8 reports with expediente codes (CR-A-C-01-2017 etc.)
  Declaraciones Provisionales (provisional reports):
    ~50 PDFs across 19+ unique registrations (through 2025)
    One row per OCCURRENCE: when a final exists, the final wins;
    a provisional builds a row ONLY for occurrences with no final.

case_id = 'dgaccr-' + expediente (e.g. dgaccr-cr-a-c-01-2017) when present,
          else 'dgaccr-' + reg.lower() + '-' + year (e.g. dgaccr-ti-sab-1990)

CDX discovery: prefix query on sub.dgac.go.cr/wp-content/uploads/ filtering
for INFORME-FINAL / DECLARACION / Informe-Final patterns.

Politeness: 2 s base delay, exponential backoff on 429/503 (30-60 s, 3 retries).
"""

import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex, tempfile

DGAC_HOST = "sub.dgac.go.cr"
UPLOADS_BASE = f"https://{DGAC_HOST}/wp-content/uploads/"

WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 2.0        # base inter-request delay
FLOOR = 80         # minimum chars to consider text usable
HOME = os.path.expanduser("~/dgaccr-ingest")
DB = os.path.join(HOME, "dgaccr.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# Keywords in filenames that identify investigation reports (case-insensitive)
_ACCIDENT_KEYWORDS = [
    "INFORME-FINAL-APROBADO",
    "INFORME-FINAL-APROBADO-MATR",
    "Informe-Final-N",          # N253CX final
    "Informe-Final-CR-ACC",     # B757 DHL
    "Proyecto-de-Informe-Final",  # HK-5228 draft-final
    "DECLARACION-PROVISIONAL",
    "Declaracion-Provisional",
    "SEGUNDA-DECLARACION-PROVISIONAL",
    "Segunda-declaracion-provisional",
]

# Filename snippets that are NOT accident investigation reports to skip
_NOT_ACCIDENT = [
    "Informe-final-Federico",
    "Informe-Final-de-Gestio",
    "Informe-final-de-gestion",
    "Informe-Final-de-Gestion",
    "Informe-final-Labores",
    "Informe-Final-Labores",
    "INFORME-FINAL-DE-GESTIO",
    "Informe-de-cierre",
    "INFORME-DE-FINALIZACION-DE-GESTION",
    "Informe-Evaluacion-Anual",
    "INFORME-EVALUACION-ANUAL",
    "INFORME-DE-EVALUACION-ANUAL",
    "Informe-SEVRI",
    "INFORME-AI-",          # internal audit
    "INFORME AI-",
    "InformedeAtestiguamiento",
    "FE-DE-ERRATAS",        # errata, not a report
    "Informe-NTSB",         # NTSB translated report (foreign, not CR)
    "OFGI-FG-OF",           # mgmt farewell report
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS dgaccr_reports (
  case_id        TEXT PRIMARY KEY,
  expediente     TEXT,              -- e.g. CR-A-C-01-2017 (NULL if not extractable)
  registration   TEXT,
  source_url     TEXT,              -- original sub.dgac.go.cr URL
  archive_url    TEXT,
  archive_ts     TEXT,
  pdf_path       TEXT,
  report_type    TEXT,              -- 'final' or 'provisional'
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang           TEXT DEFAULT 'es',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS dgaccr_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'CR',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'es',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_dgaccr_status ON dgaccr_reports(status);
CREATE INDEX IF NOT EXISTS idx_dgaccr_expediente ON dgaccr_reports(expediente);
"""

# Spanish month names
_ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_ES_MONTH_PAT = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(sorted(_ES_MONTHS, key=len, reverse=True)) + r")\s+(?:del?\s+)?(\d{4})\b",
    re.IGNORECASE,
)
# "el 15/07/2019" or plain DD/MM/YYYY
_DATE_RE = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[./\-]([01]?\d)[./\-]((?:19|20)\d{2})\b"
)
# Registration patterns: TI- prefix (Costa Rica) or foreign (N, HK, HP, D-, F-, etc.)
_REG_RE = re.compile(
    r"\b(TI-[A-Z]{3}|N\d{3,5}[A-Z]{0,2}|HK[-\s]?\d{3,5}|HP-\d{4}[A-Z]*"
    r"|[A-Z]{1,2}-[A-Z]{3,5}|UL-TI-\d+)\b"
)
# Expediente pattern in filenames and text: CR-X-X-NN-YYYY
_EXP_RE = re.compile(
    r"\b(CR-(?:A|ACC|AS|IG|IGBS|ACC)-(?:C|P|BS|CO|AG|IT|UL|UL\.CO|PR|ACCID|UL\.PR)-\d{2}-\d{4})\b",
    re.IGNORECASE,
)
# Looser expediente in text: "Expediente" followed by the code
_EXP_TEXT_RE = re.compile(r"[Ee]xpediente[:\s]+([A-Z0-9._-]{8,30})")


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
        timeout=120.0,
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


def cdx_best_snapshot(cl, url, retries=3):
    """Return the timestamp of the best (largest) snapshot for a URL.

    'Best' = highest Content-Length among status=200 captures — same logic as
    kbsz_scraper (prefer the most-complete capture, not just the latest).
    Returns None if no 200 snapshot found.
    """
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,statuscode,length",
        "filter": "statuscode:200",
        "limit": "20",
    }
    for attempt in range(retries):
        try:
            r = cl.get(CDX_BASE, params=params, timeout=30)
            if r.status_code in (429, 503):
                time.sleep(30 * (2 ** attempt))
                continue
            time.sleep(DELAY)
            data = r.json()
            if len(data) <= 1:   # only header row
                return None
            # Pick snapshot with maximum length
            best = max(data[1:], key=lambda row: int(row[2]) if row[2].isdigit() else 0)
            return best[0]       # timestamp string
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(30)
            else:
                print(f"  [cdx] error for {url}: {e}", file=sys.stderr)
                return None
    return None


def _is_accident_report(url):
    """Return True if the URL looks like an accident investigation PDF."""
    fn = urllib.parse.unquote(url).split("/")[-1]
    # Check not-accident list first
    for skip in _NOT_ACCIDENT:
        if skip.lower() in fn.lower():
            return False
    # Check positive keywords
    for kw in _ACCIDENT_KEYWORDS:
        if kw.lower() in fn.lower():
            return True
    return False


def _extract_expediente(fn):
    """Extract expediente code from a filename (URL-decoded)."""
    m = _EXP_RE.search(fn)
    if m:
        return m.group(1).upper()
    return None


def _extract_registration(fn):
    """Extract primary registration from filename."""
    fn_upper = fn.upper()
    # TI- (Costa Rican) priority
    m = re.search(r"\b(TI-[A-Z]{3})\b", fn_upper)
    if m:
        return m.group(1)
    # N-number
    m = re.search(r"\b(N\d{3,5}[A-Z]{0,2})\b", fn_upper)
    if m:
        return m.group(1)
    # HK (Colombian)
    m = re.search(r"\b(HK-?\d{3,5})\b", fn_upper)
    if m:
        return m.group(1).replace(" ", "").replace("HK", "HK-").rstrip("-")
    # HP (Panamanian)
    m = re.search(r"\b(HP-\d{4}[A-Z]*)\b", fn_upper)
    if m:
        return m.group(1)
    # UL- (ultralight)
    m = re.search(r"\b(UL-TI-\d+)\b", fn_upper)
    if m:
        return m.group(1)
    return None


def _report_type(fn):
    fn_up = fn.upper()
    if "INFORME-FINAL" in fn_up or "INFORME FINAL" in fn_up or "PROYECTO-DE-INFORME" in fn_up:
        return "final"
    if "DECLARACION" in fn_up or "DECLARACI" in fn_up:
        return "provisional"
    return "provisional"


def _case_id(expediente, registration, year=None):
    """Build a stable case_id.

    Prefer expediente when available. Fall back to reg+year.
    """
    if expediente:
        return "dgaccr-" + expediente.lower().replace(".", "-")
    if registration and year:
        return f"dgaccr-{registration.lower().replace(' ', '-')}-{year}"
    if registration:
        return f"dgaccr-{registration.lower().replace(' ', '-')}"
    return None


def _year_from_url(url):
    """Extract year from WP upload path (…/YYYY/MM/…)."""
    m = re.search(r"/uploads/(\d{4})/", url)
    return m.group(1) if m else None


# ---- DISCOVER ----------------------------------------------------------------

def discover(c, cl):
    """CDX prefix scan for all PDFs under sub.dgac.go.cr/wp-content/uploads/."""
    print("[dgaccr discover] querying CDX …", flush=True)

    params = {
        "url": f"{UPLOADS_BASE}",
        "matchType": "prefix",
        "output": "json",
        "fl": "timestamp,original,statuscode,length",
        "filter": ["statuscode:200", "mimetype:application/pdf"],
        "collapse": "digest",
        "limit": "2000",
    }
    all_rows = []
    for offset in [0, 500, 1000, 1500]:
        params_page = dict(params)
        params_page["offset"] = str(offset)
        try:
            r = cl.get(CDX_BASE, params=params_page, timeout=60)
            time.sleep(DELAY)
            data = r.json()
            if len(data) <= 1:
                break
            all_rows.extend(data[1:])
            if len(data) < 502:   # last page
                break
        except Exception as e:
            print(f"  [cdx] page offset={offset} error: {e}", file=sys.stderr)
            break

    # Deduplicate by URL
    seen_urls = {}
    for row in all_rows:
        ts, url, sc, length = row
        if url not in seen_urls or int(length or 0) > int(seen_urls[url][3] or 0):
            seen_urls[url] = row

    print(f"  CDX returned {len(seen_urls)} unique PDF URLs", flush=True)

    # Filter to accident investigation reports only
    investigation_rows = [(ts, url, sc, lg) for ts, url, sc, lg in seen_urls.values()
                          if _is_accident_report(url)]
    print(f"  {len(investigation_rows)} match accident-investigation filter", flush=True)

    inserted = 0
    for ts, url, sc, length in investigation_rows:
        fn = urllib.parse.unquote(url.split("/")[-1])
        expediente = _extract_expediente(fn)
        registration = _extract_registration(fn)
        year = _year_from_url(url)
        rtype = _report_type(fn)
        cid = _case_id(expediente, registration, year)

        if not cid:
            print(f"  [warn] no case_id for {fn}", file=sys.stderr)
            continue

        # Supersession logic: finals win over provisionals for same expediente
        existing = c.execute(
            "SELECT case_id, report_type, source_url FROM dgaccr_reports WHERE case_id=?",
            (cid,)
        ).fetchone()

        if existing:
            # If existing is final and incoming is provisional → skip
            if existing["report_type"] == "final" and rtype == "provisional":
                continue
            # If existing is provisional and incoming is final → upgrade
            if existing["report_type"] == "provisional" and rtype == "final":
                c.execute(
                    "UPDATE dgaccr_reports SET source_url=?, archive_ts=?, report_type=?, "
                    "registration=?, expediente=?, status='new', skip_reason=NULL, updated_at=? "
                    "WHERE case_id=?",
                    (url, ts, rtype, registration or existing["registration"],
                     expediente or existing["expediente"], now(), cid),
                )
                c.commit()
            continue

        c.execute(
            "INSERT OR IGNORE INTO dgaccr_reports "
            "(case_id, expediente, registration, source_url, archive_ts, report_type, "
            "lang, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'es', 'new', ?, ?)",
            (cid, expediente, registration, url, ts, rtype, now(), now()),
        )
        c.commit()
        inserted += 1

    print(f"[dgaccr discover] inserted {inserted} new rows", flush=True)
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c, cl):
    """Download PDFs via Wayback Machine using CDX best-snapshot selection.

    Large files (>5 MB) are fetched with curl to avoid httpx memory pressure.
    """
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url, archive_ts FROM dgaccr_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    not_archived = []

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[dgaccr fetch] {cid} …", flush=True)

        # Already on disk?
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            c.execute(
                "UPDATE dgaccr_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
            continue

        # CDX best snapshot
        ts = row["archive_ts"] or cdx_best_snapshot(cl, orig_url)
        if not ts:
            print(f"  not archived → recheck later", flush=True)
            not_archived.append(cid)
            c.execute(
                "UPDATE dgaccr_reports SET status='skipped', skip_reason='not-archived', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
        print(f"  ts={ts} url={archive_url}", flush=True)

        # Use curl for known large finals (TI-AGM ~30MB, TI-SAB ~17MB, TI-LRC ~8MB)
        use_curl = any(k in cid for k in ["ti-agm", "ti-sab", "ti-lrc"])

        ok = False
        if use_curl:
            # curl for big files — expect minutes
            print(f"  using curl (large file expected)", flush=True)
            cp = subprocess.run(
                ["curl", "-L", "--max-time", "600", "-A", UA,
                 "-o", dest, archive_url],
                capture_output=True,
            )
            if cp.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 500:
                ok = True
            else:
                print(f"  curl exit={cp.returncode}", file=sys.stderr)
        else:
            try:
                r = wayback_get(cl, archive_url)
                if not r or r.status_code != 200:
                    print(f"  HTTP {r.status_code if r else 'none'}", file=sys.stderr)
                    c.execute(
                        "UPDATE dgaccr_reports SET status='skipped', "
                        "skip_reason=?, updated_at=? WHERE case_id=?",
                        (f"http-{r.status_code if r else 'none'}", now(), cid),
                    )
                    c.commit()
                    continue

                # Check PDF magic — handle Wayback HTML wrapper for id_ failures
                content = r.content
                if content[:4] != b"%PDF":
                    # Strip possible Wayback HTML wrapper (happens on TI-BIL sometimes)
                    pdf_start = content.find(b"%PDF")
                    if pdf_start > 0:
                        content = content[pdf_start:]
                        print(f"  stripped {pdf_start}B Wayback wrapper", flush=True)
                    else:
                        # Try without id_ flag (may return 200 with HTML wrapper)
                        archive_url_noid = f"{WAYBACK_BASE}/{ts}/{orig_url}"
                        print(f"  not PDF, retrying without id_ flag …", flush=True)
                        r2 = wayback_get(cl, archive_url_noid)
                        if r2 and r2.status_code == 200:
                            content = r2.content
                            pdf_start = content.find(b"%PDF")
                            if pdf_start > 0:
                                content = content[pdf_start:]
                        if content[:4] != b"%PDF":
                            print(f"  still not PDF ({content[:20]!r})", file=sys.stderr)
                            c.execute(
                                "UPDATE dgaccr_reports SET status='skipped', "
                                "skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                                (now(), cid),
                            )
                            c.commit()
                            continue

                with open(dest, "wb") as fh:
                    fh.write(content)
                ok = True

            except Exception as e:
                print(f"  exception: {e}", file=sys.stderr)
                c.execute(
                    "UPDATE dgaccr_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (str(e)[:120], now(), cid),
                )
                c.commit()
                continue

        if ok:
            size = os.path.getsize(dest)
            # Final verify: even curl output might be HTML on some misses
            with open(dest, "rb") as fh:
                magic = fh.read(4)
            if magic != b"%PDF":
                print(f"  file is not PDF after download ({magic!r})", file=sys.stderr)
                os.unlink(dest)
                c.execute(
                    "UPDATE dgaccr_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

            c.execute(
                "UPDATE dgaccr_reports SET pdf_path=?, archive_url=?, archive_ts=?, "
                "status='fetched', updated_at=? WHERE case_id=?",
                (dest, archive_url, ts, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved {size//1024}KB", flush=True)

    print(f"[dgaccr fetch] downloaded={downloaded} not_archived={len(not_archived)}", flush=True)
    return downloaded, not_archived


# ---- PARSE -------------------------------------------------------------------

def extract_text(pdf_path):
    """Extract text from PDF using pdftotext (layout mode)."""
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=120,
        )
        return cp.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def parse_date(txt):
    """Parse OCCURRENCE date from Spanish PDF text. Returns ISO YYYY-MM-DD or None.

    Priority:
    1. Date embedded in occurrence title line: "el día DD de MES de YYYY" or
       "ocurrido … el DD de MES de YYYY" (most reliable — skips cover/update dates)
    2. General "DD de MES de YYYY" in first 3000 chars, skipping the header
       "Al NN de MES del YYYY" line (which is the document update date, not the event).
    3. Numeric DD/MM/YYYY or YYYY-MM-DD.
    """
    if not txt:
        return None

    # Weekday names to skip in date patterns
    _WEEKDAY = r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+"

    # Pattern 0: explicit "Fecha del evento:" or "Fecha del accidente:" label — most authoritative
    # Handles both "06 de octubre del 2020" and "martes 06 de octubre del 2020"
    _FECHA_EVENTO = re.compile(
        r"Fecha\s+(?:y\s+hora\s+)?del?\s+(?:evento|accidente|ocurrencia|suceso)[:\s]+"
        + r"(?:" + _WEEKDAY + r")?"
        + r"(\d{1,2})\s+de\s+("
        + "|".join(sorted(_ES_MONTHS, key=len, reverse=True))
        + r")\s+(?:del?\s+)?(\d{4})\b",
        re.IGNORECASE,
    )
    m = _FECHA_EVENTO.search(txt[:8000])
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(_ES_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = _ES_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Pattern 0b: "Fecha del evento: DD/MM/YYYY"
    _FECHA_EVENTO_NUM = re.compile(
        r"Fecha\s+del\s+(?:evento|accidente|ocurrencia|suceso)[:\s]+(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b",
        re.IGNORECASE,
    )
    m = _FECHA_EVENTO_NUM.search(txt[:8000])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Pattern 1: embedded in title line — "el día 30 de diciembre de 2020" or
    #            "el martes 06 de octubre del 2020"
    _TITLE_DATE = re.compile(
        r"\bel\s+(?:d[ií]a\s+)?(?:" + _WEEKDAY + r")?(\d{1,2})\s+de\s+("
        + "|".join(sorted(_ES_MONTHS, key=len, reverse=True))
        + r")\s+(?:del?\s+)?(\d{4})\b",
        re.IGNORECASE,
    )
    m = _TITLE_DATE.search(txt[:4000])
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(_ES_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = _ES_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Pattern 2: "ocurrido el / ocurrencia … DD de MES de YYYY"
    _OCURR_DATE = re.compile(
        r"ocurri[oó]\w*\s+(?:el\s+)?(\d{1,2})\s+de\s+(" + "|".join(sorted(_ES_MONTHS, key=len, reverse=True)) + r")\s+(?:del?\s+)?(\d{4})\b",
        re.IGNORECASE,
    )
    m = _OCURR_DATE.search(txt[:5000])
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(_ES_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = _ES_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Pattern 3: general Spanish date, but skip "Al NN de MES del YYYY" lines (cover update date)
    header = txt[:3000]
    # Remove "Al DD de MES del YYYY" lines to avoid picking up update date
    header_clean = re.sub(
        r"\bAl\s+\d{1,2}\s+de\s+\w+\s+del?\s+\d{4}\b", "", header, flags=re.IGNORECASE
    )
    m = _ES_MONTH_PAT.search(header_clean)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(_ES_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = _ES_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Pattern 4: Numeric: DD/MM/YYYY or YYYY-MM-DD
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
    """Extract primary aircraft registration from text (prefer TI-).

    Explicitly ignores expediente-like codes (CR-A-..., CR-ACC-...) which
    could be parsed as registration-like patterns by the generic regex.
    """
    if not txt:
        return None
    header = txt[:4000]
    # TI- (Costa Rican civil) priority
    m = re.search(r"\b(TI-[A-Z]{3})\b", header)
    if m:
        return m.group(1)
    # N-number (US)
    m = re.search(r"\b(N\d{3,5}[A-Z]{0,2})\b", header)
    if m:
        return m.group(1)
    # HK- (Colombian)
    m = re.search(r"\b(HK[-\s]\d{3,5})\b", header)
    if m:
        return m.group(1).replace(" ", "-")
    # HP- (Panamanian)
    m = re.search(r"\b(HP-\d{4}[A-Z]*)\b", header)
    if m:
        return m.group(1)
    # UL-TI- (ultralight)
    m = re.search(r"\b(UL-TI-\d+)\b", header)
    if m:
        return m.group(1)
    # D- (German), F- (French) — foreign registrations in some events
    m = re.search(r"\b([DF]-[A-Z]{4})\b", header)
    if m:
        return m.group(1)
    return None


def parse_aircraft(txt):
    """Extract aircraft type from Spanish investigation report text."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r"(?:Tipo|Marca\s*y\s*Modelo|Aeronave|tipo\s+de\s+aeronave)[:\s]+([A-Za-z0-9][\w\s\-/]{2,50})",
        r"(?:Aircraft[:\s]+|Tipo de aeronave[:\s]+)([A-Za-z0-9][\w\s\-/]{2,40})",
        r"\b(Cessna\s+[\w\-]+|Piper\s+[\w\-]+|Beechcraft\s+[\w\-]+|Boeing\s+[\w\-]+|Airbus\s+[A-Z]\d[\w\-]+)",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 60:
                return v
    return None


def parse_operator(txt):
    """Extract operator from Spanish report."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r"(?:Operador|Explotador|Propietario)[:\s]+([A-Za-zÁÉÍÓÚáéíóúñÑ][\w\s\-,.]{3,60})",
        r"(?:Operator)[:\s]+([A-Za-z][\w\s\-,.]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 80:
                return v
    return None


def parse_location(txt):
    """Extract occurrence location from Spanish report."""
    if not txt:
        return None
    header = txt[:5000]
    for pat in [
        r"(?:Lugar\s+del\s+[Aa]ccidente|Lugar\s+de\s+ocurrencia|Lugar)[:\s]+([A-Za-zÁÉÍÓÚáéíóúñÑ][\w\s\-,./]{3,80})",
        r"(?:Location)[:\s]+([A-Za-z][\w\s\-,./]{3,80})",
        r"ocurri[oó]\s+(?:el\s+)?(?:\d+\s+de\s+\w+\s+\w+\s+)?(?:de\s+\d{4}\s+)?(?:en|cerca\s+de)\s+([A-Za-zÁÉÍÓÚáéíóúñÑ][\w\s\-,./]{3,80})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 3 < len(v) < 100:
                return v
    return None


def parse_probable_cause(txt):
    """Extract probable cause section from Spanish report."""
    if not txt:
        return None
    m = re.search(
        r"(?:Causa\s+[Pp]robable|Causas?\s+[Pp]robables?)[:\s]*\n?(.*?)(?:\n{2,}|\Z)",
        txt[:8000], re.DOTALL | re.IGNORECASE,
    )
    if m:
        v = m.group(1).strip()
        if 10 < len(v) < 2000:
            return v
    return None


def _strip_arabic(txt):
    """Remove Arabic-script blocks from text (not needed for ES source, but kept for safety)."""
    # Costa Rica reports are pure Spanish; this is a no-op here
    return txt


def parse(c):
    """Extract text + metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, report_type FROM dgaccr_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[dgaccr parse] {cid}", flush=True)

        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  PDF missing", file=sys.stderr)
            continue

        txt = extract_text(pdf_path)
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"  pdftotext → {len(txt)} chars (file={size_kb}KB)", flush=True)

        if len(txt) < FLOOR:
            print(f"  insufficient text → skip (photo-heavy PDF?)", file=sys.stderr)
            c.execute(
                "UPDATE dgaccr_reports SET narrative_text=?, status='skipped', "
                "skip_reason='no-text', updated_at=? WHERE case_id=?",
                (txt, now(), cid),
            )
            c.commit()
            continue

        event_date = parse_date(txt)
        registration = parse_registration(txt) or row.get("registration")
        aircraft = parse_aircraft(txt)
        location = parse_location(txt)
        operator = parse_operator(txt)
        probable_cause = parse_probable_cause(txt)

        # Try to extract expediente from text if not already known
        expediente = c.execute(
            "SELECT expediente FROM dgaccr_reports WHERE case_id=?", (cid,)
        ).fetchone()
        if expediente and not expediente["expediente"]:
            m = _EXP_TEXT_RE.search(txt[:3000])
            if m:
                exp = m.group(1).strip().upper()
                c.execute(
                    "UPDATE dgaccr_reports SET expediente=? WHERE case_id=?",
                    (exp, cid),
                )

        c.execute(
            """UPDATE dgaccr_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?, operator=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft,
             location, operator, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  date={event_date} reg={registration}", flush=True)

    print(f"[dgaccr parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def build(c):
    """Write dgaccr_accidents from parsed rows.

    Supersession: finals win over provisionals for same expediente.
    A provisional row is only written if no final exists for the same occurrence.
    """
    # First pass: collect all finals by expediente
    finals_by_exp = {}
    for r in c.execute(
        "SELECT case_id, expediente, registration FROM dgaccr_reports "
        "WHERE status='parsed' AND report_type='final'"
    ):
        exp = r["expediente"] or r["case_id"]
        finals_by_exp[exp] = r["case_id"]

    # Second pass: for provisionals with same expediente, keep only the most recent
    # (highest archive_ts) — multiple provisional updates of same occurrence
    prov_by_exp = {}   # expediente → (case_id, archive_ts)
    for r in c.execute(
        "SELECT case_id, expediente, archive_ts FROM dgaccr_reports "
        "WHERE status='parsed' AND report_type='provisional' AND expediente IS NOT NULL"
    ):
        exp = r["expediente"]
        ts = r["archive_ts"] or ""
        if exp not in prov_by_exp or ts > prov_by_exp[exp][1]:
            prov_by_exp[exp] = (r["case_id"], ts)
    # Set of case_ids that are the LATEST provisional per expediente
    latest_prov = {cid for cid, _ in prov_by_exp.values()}

    rows = c.execute(
        """SELECT case_id, expediente, report_type, event_date, aircraft,
                  registration, operator, location, narrative_text,
                  probable_cause, source_url, lang
           FROM dgaccr_reports WHERE status='parsed'"""
    ).fetchall()

    built = 0
    skipped_superseded = 0

    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE dgaccr_reports SET status='skipped', skip_reason='no-text', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        # Supersession: skip provisional if a final exists for the same occurrence
        if r["report_type"] == "provisional" and r["expediente"]:
            if r["expediente"] in finals_by_exp and finals_by_exp[r["expediente"]] != r["case_id"]:
                print(f"  [build] skipping provisional {r['case_id']} (final exists)", flush=True)
                c.execute(
                    "UPDATE dgaccr_reports SET status='skipped', skip_reason='superseded-by-final', "
                    "updated_at=? WHERE case_id=?",
                    (now(), r["case_id"]),
                )
                c.commit()
                skipped_superseded += 1
                continue
            # Also skip older provisional updates for same expediente
            if r["expediente"] in prov_by_exp and prov_by_exp[r["expediente"]][0] != r["case_id"]:
                print(f"  [build] skipping older provisional {r['case_id']} (newer update exists)", flush=True)
                c.execute(
                    "UPDATE dgaccr_reports SET status='skipped', skip_reason='superseded-by-newer-provisional', "
                    "updated_at=? WHERE case_id=?",
                    (now(), r["case_id"]),
                )
                c.commit()
                skipped_superseded += 1
                continue

        cid = r["case_id"]
        slug = cid.lower()

        c.execute(
            """INSERT OR REPLACE INTO dgaccr_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'CR', ?, ?, ?, ?, ?, ?, ?)""",
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
                "Final report" if r["report_type"] == "final" else "Declaración Provisional",
                slug,
                r["lang"] or "es",
                now(),
            ),
        )
        c.execute(
            "UPDATE dgaccr_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[dgaccr build] built={built} skipped_superseded={skipped_superseded}", flush=True)
    return built


# ---- RECHECK -----------------------------------------------------------------

def recheck(c, cl):
    """Re-query CDX for not-archived PDFs."""
    rows = c.execute(
        "SELECT case_id, source_url FROM dgaccr_reports "
        "WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    print(f"[dgaccr recheck] checking {len(rows)} not-archived URLs", flush=True)
    newly_available = 0
    for row in rows:
        ts = cdx_best_snapshot(cl, row["source_url"])
        if ts:
            print(f"  {row['case_id']} now has snapshot ts={ts}", flush=True)
            c.execute(
                "UPDATE dgaccr_reports SET status='new', archive_ts=?, skip_reason=NULL, "
                "updated_at=? WHERE case_id=?",
                (ts, now(), row["case_id"]),
            )
            c.commit()
            newly_available += 1
    print(f"[dgaccr recheck] newly_available={newly_available}", flush=True)
    return newly_available


# ---- STATS -------------------------------------------------------------------

def print_stats(c):
    print("\n--- dgaccr_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM dgaccr_reports GROUP BY status, skip_reason"
    ):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):30s} {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM dgaccr_accidents").fetchone()[0]
    print(f"\n--- dgaccr_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM dgaccr_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM dgaccr_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  narr_len min={narr[0]} max={narr[1]}")
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, report_type, LENGTH(narrative_text) len "
            "FROM dgaccr_accidents ORDER BY event_date NULLS LAST LIMIT 10"
        ):
            print(f"    {r['case_id']:40s}  reg={r['registration'] or 'NULL':12s}  "
                  f"date={r['event_date'] or 'NULL'}  type={r['report_type']:12s}  narr={r['len']}")

    # Not-archived
    na = c.execute(
        "SELECT case_id FROM dgaccr_reports WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    if na:
        print(f"\n  not-archived: {[r['case_id'] for r in na]}")


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

    if mode in ("build", "all"):
        build(c)

    if mode == "recheck":
        cl = http()
        try:
            newly = recheck(c, cl)
            if newly:
                print("Run 'fetch' → 'parse' → 'build' to ingest newly available.")
        finally:
            cl.close()

    print_stats(c)


if __name__ == "__main__":
    main()
