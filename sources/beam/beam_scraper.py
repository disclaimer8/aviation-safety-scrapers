#!/usr/bin/env python3
"""beam (Morocco BEAM — Bureau d'Enquêtes et d'Analyses, country MA, lang 'fr')
aviation-accident ingest.

Source: bea.aviationcivile.gov.ma — recovered from the Wayback Machine because
the live site is restricted to MA IPs.
DO NOT probe bea.aviationcivile.gov.ma directly.

12 investigation reports found:
  /assets/doc/ path (9 reports):
    Rapport Final CN-CDP.pdf
    Rapport Final CN-HBA.pdf
    Rapport final-HB CAD.pdf
    Rapport-final-CN-ROR_c.pdf
    Rapport-Final_CN-RNZ.pdf
    Rapport-Final_FG-RXC_compressed.pdf   (Air France A320, 2011-08-08)
    Report-CN-COH.pdf
    Report-CN-NMH.pdf
    Report-CN-RNW.pdf
  /portail/web/uploads/images/ path (3 reports):
    3115de2f26e0f17766e224055b8bdbf8.pdf
    822e92f154b6d702f9cbd17a740463b2.pdf   (BEAM_20072019_01, Cessna C177)
    Rapport FINAL_ CN-TCG.pdf

case_id = 'beam-' + BEAM reference (BEAM_YYYYMMDD_NN format when found in text)
          else 'beam-' + registration.lower()

All reports are bilingual FR+AR. Strategy:
  - pdftotext extracts FR and AR mixed
  - Strip Arabic-script blocks (Unicode ranges 0600-06FF, 0750-077F, FB50-FDFF, FE70-FEFF)
    from the narrative_text field, keeping only the French body
  - If a PDF yields only Arabic text after stripping, skip + report

CDX: prefix scan on both /assets/doc/ and /portail/web/uploads/images/ with
     best (max-length) capture selection.

Politeness: 2s base delay, exponential backoff on 429/503.
"""

import sys, os, re, time, sqlite3, subprocess, json, urllib.parse

BEA_HOST = "bea.aviationcivile.gov.ma"
ASSETS_URL = f"https://{BEA_HOST}/assets/doc/"
PORTAIL_URL = f"https://{BEA_HOST}/portail/web/uploads/images/"

WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 2.0
FLOOR = 80           # minimum chars to consider text usable (after Arabic stripping)
HOME = os.path.expanduser("~/beam-ingest")
DB = os.path.join(HOME, "beam.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# Non-accident PDFs to skip in assets/doc/
_NOT_REPORT = [
    "Decret",
    "InstrTech",
    "Loi",
]

# Known explicit mapping: filename → (registration, BEAM_ref, event_date_hint)
# Used when text extraction is unreliable for metadata
_KNOWN = {
    "Rapport%20Final%20CN-CDP.pdf":            ("CN-CDP",  None,             None),
    "Rapport Final CN-CDP.pdf":                ("CN-CDP",  None,             None),
    "Rapport%20Final%20CN-HBA.pdf":            ("CN-HBA",  None,             None),
    "Rapport Final CN-HBA.pdf":                ("CN-HBA",  None,             None),
    "Rapport%20final-HB%20CAD.pdf":            ("HB-CAD",  None,             None),
    "Rapport final-HB CAD.pdf":                ("HB-CAD",  None,             None),
    "Rapport-final-CN-ROR_c.pdf":              ("CN-ROR",  None,             None),
    "Rapport-Final_CN-RNZ.pdf":                ("CN-RNZ",  None,             None),
    "Rapport-Final_FG-RXC_compressed.pdf":     ("F-GRXC",  None,             None),
    "Report-CN-COH.pdf":                       ("CN-COH",  None,             None),
    "Report-CN-NMH.pdf":                       ("CN-NMH",  None,             None),
    "Report-CN-RNW.pdf":                       ("CN-RNW",  None,             None),
    "822e92f154b6d702f9cbd17a740463b2.pdf":    ("CN-TKW",  "BEAM_20072019_01", "2019-07-20"),
    "3115de2f26e0f17766e224055b8bdbf8.pdf":    ("OO-JAY",  "BEAM_02052018-01", "2018-05-02"),
    "Rapport%20FINAL_%20CN-TCG.pdf":           ("CN-TCG",  None,             None),
    "Rapport FINAL_ CN-TCG.pdf":               ("CN-TCG",  None,             None),
}

# Arabic script Unicode blocks (to strip from narrative)
_ARABIC_RE = re.compile(
    r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+"
    r"[\s؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]*",
    re.UNICODE,
)

# French month names
_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_FR_MONTH_PAT = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(sorted(_FR_MONTHS, key=len, reverse=True)) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
# "survenu le 20/07/2019"
_DATE_SURVENU = re.compile(
    r"survenu\s+le\s+(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[./\-]([01]?\d)[./\-]((?:19|20)\d{2})\b"
)
# BEAM reference: BEAM_YYYYMMDD_NN
_BEAM_REF_RE = re.compile(r"\bBEAM_(\d{8}_\d{2})\b")
# Aircraft registration (Moroccan CN-*, foreign registrations)
_REG_RE = re.compile(
    r"\b(CN-[A-Z]{3}|[A-Z]-[A-Z]{4}|[A-Z]{2}-[A-Z]{3,4}|F-G[A-Z]{3}|HB-[A-Z]{3}|N\d{3,5}[A-Z]{0,2})\b"
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS beam_reports (
  case_id        TEXT PRIMARY KEY,
  beam_ref       TEXT,              -- BEAM_YYYYMMDD_NN when found in text
  registration   TEXT,
  source_url     TEXT,
  archive_url    TEXT,
  archive_ts     TEXT,
  pdf_path       TEXT,
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,              -- French body only (Arabic stripped)
  probable_cause TEXT,
  lang           TEXT DEFAULT 'fr',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS beam_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'MA',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT DEFAULT 'Rapport Final',
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_beam_status ON beam_reports(status);
"""


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
    """Return (timestamp, length) of the best (largest 200) snapshot."""
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
            if len(data) <= 1:
                return None, 0
            best = max(data[1:], key=lambda row: int(row[2]) if row[2].isdigit() else 0)
            return best[0], int(best[2]) if best[2].isdigit() else 0
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(30)
            else:
                print(f"  [cdx] error for {url}: {e}", file=sys.stderr)
                return None, 0
    return None, 0


def _is_report_url(url):
    fn = urllib.parse.unquote(url.split("/")[-1])
    for skip in _NOT_REPORT:
        if fn.startswith(skip):
            return False
    return fn.lower().endswith(".pdf")


def _registration_from_url(url):
    """Extract registration from URL/filename using known mapping or pattern."""
    fn_decoded = urllib.parse.unquote(url.split("/")[-1])
    # Try known mapping first (both encoded and decoded forms)
    for key in [url.split("/")[-1], fn_decoded]:
        if key in _KNOWN:
            reg, beam_ref, _ = _KNOWN[key]
            return reg, beam_ref
    # Pattern match: CN-XXX, HB-XXX, F-GXXX, etc. in filename
    m = _REG_RE.search(fn_decoded)
    if m:
        return m.group(1), None
    return None, None


def _case_id_from(registration, beam_ref):
    if beam_ref:
        return "beam-" + beam_ref.lower()
    if registration:
        return "beam-" + registration.lower().replace(" ", "-")
    return None


# ---- DISCOVER ----------------------------------------------------------------

def discover(c, cl):
    """CDX prefix scan for both BEAM URL paths."""
    print("[beam discover] querying CDX …", flush=True)
    all_rows = []

    for base_url in [ASSETS_URL, PORTAIL_URL]:
        params = {
            "url": base_url,
            "matchType": "prefix",
            "output": "json",
            "fl": "timestamp,original,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "digest",
            "limit": "200",
        }
        try:
            r = cl.get(CDX_BASE, params=params, timeout=60)
            time.sleep(DELAY)
            data = r.json()
            if len(data) > 1:
                all_rows.extend(data[1:])
                print(f"  {base_url}: {len(data)-1} rows", flush=True)
        except Exception as e:
            print(f"  CDX error for {base_url}: {e}", file=sys.stderr)

    # Deduplicate by URL, prefer largest capture
    seen = {}
    for ts, url, sc, length in all_rows:
        if not url.lower().endswith(".pdf"):
            continue
        if not _is_report_url(url):
            continue
        cur_len = int(length or 0)
        if url not in seen or cur_len > int(seen[url][3] or 0):
            seen[url] = (ts, url, sc, length)

    print(f"  {len(seen)} unique investigation PDF URLs after filter", flush=True)

    inserted = 0
    for ts, url, sc, length in seen.values():
        reg, beam_ref = _registration_from_url(url)
        cid = _case_id_from(reg, beam_ref)

        if not cid:
            # Fall back to hash-based case_id for hash-named files
            fn = url.split("/")[-1].replace(".pdf", "")
            cid = "beam-" + re.sub(r"[^A-Za-z0-9]", "-", fn)[:40].lower()
            print(f"  [warn] no reg/ref for {url.split('/')[-1]} → cid={cid}", file=sys.stderr)

        existing = c.execute(
            "SELECT case_id FROM beam_reports WHERE case_id=?", (cid,)
        ).fetchone()
        if existing:
            continue

        c.execute(
            "INSERT OR IGNORE INTO beam_reports "
            "(case_id, beam_ref, registration, source_url, archive_ts, "
            "lang, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'fr', 'new', ?, ?)",
            (cid, beam_ref, reg, url, ts, now(), now()),
        )
        c.commit()
        inserted += 1

    print(f"[beam discover] inserted {inserted} new rows", flush=True)
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c, cl):
    """Download PDFs via Wayback Machine."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url, archive_ts FROM beam_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    not_archived = []

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[beam fetch] {cid} …", flush=True)

        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            c.execute(
                "UPDATE beam_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
            continue

        ts = row["archive_ts"]
        if not ts:
            ts, _ = cdx_best_snapshot(cl, orig_url)
        if not ts:
            print(f"  not archived", flush=True)
            not_archived.append(cid)
            c.execute(
                "UPDATE beam_reports SET status='skipped', skip_reason='not-archived', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
        print(f"  ts={ts} url={archive_url}", flush=True)

        try:
            r = wayback_get(cl, archive_url)
            if not r or r.status_code != 200:
                code = r.status_code if r else "none"
                print(f"  HTTP {code}", file=sys.stderr)
                c.execute(
                    "UPDATE beam_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (f"http-{code}", now(), cid),
                )
                c.commit()
                continue

            content = r.content
            if content[:4] != b"%PDF":
                # Try stripping Wayback HTML wrapper
                pdf_start = content.find(b"%PDF")
                if pdf_start > 0:
                    content = content[pdf_start:]
                    print(f"  stripped {pdf_start}B Wayback wrapper", flush=True)
                else:
                    # Try without id_
                    archive_url_noid = f"{WAYBACK_BASE}/{ts}/{orig_url}"
                    print(f"  not PDF, retry without id_ …", flush=True)
                    r2 = wayback_get(cl, archive_url_noid)
                    if r2 and r2.status_code == 200:
                        content = r2.content
                        pdf_start = content.find(b"%PDF")
                        if pdf_start > 0:
                            content = content[pdf_start:]
                    if content[:4] != b"%PDF":
                        print(f"  still not PDF ({content[:20]!r})", file=sys.stderr)
                        c.execute(
                            "UPDATE beam_reports SET status='skipped', skip_reason='not-pdf', "
                            "updated_at=? WHERE case_id=?",
                            (now(), cid),
                        )
                        c.commit()
                        continue

            with open(dest, "wb") as fh:
                fh.write(content)

            size = os.path.getsize(dest)
            c.execute(
                "UPDATE beam_reports SET pdf_path=?, archive_url=?, archive_ts=?, "
                "status='fetched', updated_at=? WHERE case_id=?",
                (dest, archive_url, ts, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved {size//1024}KB", flush=True)

        except Exception as e:
            print(f"  exception: {e}", file=sys.stderr)
            c.execute(
                "UPDATE beam_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                (str(e)[:120], now(), cid),
            )
            c.commit()

    print(f"[beam fetch] downloaded={downloaded} not_archived={len(not_archived)}", flush=True)
    return downloaded, not_archived


# ---- PARSE -------------------------------------------------------------------

def extract_text(pdf_path):
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=120,
        )
        return cp.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def strip_arabic(txt):
    """Remove Arabic-script characters and surrounding whitespace from text.

    Keeps the French body intact. Lines that are >80% Arabic after stripping
    are dropped entirely to avoid garbled mixed lines.
    """
    if not txt:
        return ""
    result_lines = []
    for line in txt.split("\n"):
        # Count Arabic chars vs total non-whitespace
        arabic_chars = len(_ARABIC_RE.findall(line))
        total_chars = len(line.strip())
        if total_chars > 0 and arabic_chars / total_chars > 0.8:
            # Predominantly Arabic line — skip entirely
            continue
        # Strip Arabic from mixed lines
        clean = _ARABIC_RE.sub(" ", line)
        clean = re.sub(r"  +", " ", clean).strip()
        if clean:
            result_lines.append(clean)
    return "\n".join(result_lines)


def is_arabic_only(txt):
    """Return True if text is >90% Arabic script (skip these PDFs)."""
    if not txt:
        return True
    stripped = strip_arabic(txt)
    return len(stripped.strip()) < FLOOR


def parse_date_fr(txt):
    """Parse occurrence date from French PDF text. Returns ISO YYYY-MM-DD or None."""
    if not txt:
        return None
    header = txt[:5000]

    # "survenu le DD/MM/YYYY" or "survenu le DD-MM-YYYY"
    m = _DATE_SURVENU.search(header)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # "le DD mois YYYY" or "DD mois YYYY"
    m = _FR_MONTH_PAT.search(header)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = None
        for k in sorted(_FR_MONTHS, key=len, reverse=True):
            if month_str.startswith(k):
                mo = _FR_MONTHS[k]
                break
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    # Numeric: DD/MM/YYYY
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


def parse_beam_ref(txt):
    """Extract BEAM_YYYYMMDD_NN reference from text."""
    m = _BEAM_REF_RE.search(txt[:5000])
    return "BEAM_" + m.group(1) if m else None


def parse_registration_fr(txt, known_reg=None):
    """Extract aircraft registration from French report text."""
    if known_reg:
        return known_reg
    if not txt:
        return None
    header = txt[:5000]
    # CN- (Moroccan) priority
    m = re.search(r"\b(CN-[A-Z]{3})\b", header)
    if m:
        return m.group(1)
    # F-G (French)
    m = re.search(r"\b(F-G[A-Z]{3})\b", header)
    if m:
        return m.group(1)
    # HB- (Swiss)
    m = re.search(r"\b(HB-[A-Z]{3})\b", header)
    if m:
        return m.group(1)
    m = _REG_RE.search(header)
    return m.group(1) if m else None


def parse_aircraft_fr(txt):
    """Extract aircraft type from French report text."""
    if not txt:
        return None
    header = txt[:6000]
    for pat in [
        r"(?:Type[:\s]+|Aéronef[:\s]+|type d'aéronef[:\s]+)([A-Za-z0-9][\w\s\-/]{2,50})",
        r"\b(Cessna\s+[\w\-]+|Piper\s+[\w\-]+|Beech\w*\s+[\w\-]+|Boeing\s+[\w\-]+|Airbus\s+[A-Z]\d[\w\-]+|ATR\s*\d+|Robin\s+[\w\-]+|Robin\s+DR[\w\-]*)",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 60:
                return v
    return None


def parse_operator_fr(txt):
    """Extract operator from French report."""
    if not txt:
        return None
    header = txt[:6000]
    for pat in [
        r"(?:Exploitant|Opérateur|Propriétaire)[:\s]+([A-Za-zÀ-ÿ][\w\s\-,.]{3,60})",
        r"(?:Operator)[:\s]+([A-Za-z][\w\s\-,.]{3,60})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 2 < len(v) < 80:
                return v
    return None


def parse_location_fr(txt):
    """Extract occurrence location from French report."""
    if not txt:
        return None
    header = txt[:6000]
    for pat in [
        r"(?:Lieu[:\s]+|Lieu de l.accident[:\s]+|Localisation[:\s]+)([A-Za-zÀ-ÿ][\w\s\-,./]{3,80})",
        r"(?:survenu\s+(?:le\s+\S+\s+)?(?:à|près\s+de|en\s+)\s*)([A-Za-zÀ-ÿ][\w\s\-,./]{3,80})",
        r"(?:à|near)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s\-]{4,50}),?\s+(?:Maroc|Morocco|France)",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r"[\n\r]", m.group(1))[0].strip().strip(",;.")
            if 3 < len(v) < 100:
                return v
    return None


def parse_probable_cause_fr(txt):
    """Extract probable cause from French report."""
    if not txt:
        return None
    m = re.search(
        r"(?:Cause[s]?\s+probable[s]?|Conclusions?)[:\s]*\n?(.*?)(?:\n{2,}|\Z)",
        txt[:10000], re.DOTALL | re.IGNORECASE,
    )
    if m:
        v = m.group(1).strip()
        if 10 < len(v) < 3000:
            return v
    return None


def parse(c):
    """Extract text + metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, registration, beam_ref "
        "FROM beam_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    skipped_arabic = []

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[beam parse] {cid}", flush=True)

        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  PDF missing", file=sys.stderr)
            continue

        raw_txt = extract_text(pdf_path)
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"  pdftotext → {len(raw_txt)} chars raw (file={size_kb}KB)", flush=True)

        if len(raw_txt) < FLOOR:
            print(f"  insufficient raw text → skip (no-text)", file=sys.stderr)
            c.execute(
                "UPDATE beam_reports SET narrative_text=?, status='skipped', "
                "skip_reason='no-text', updated_at=? WHERE case_id=?",
                (raw_txt, now(), cid),
            )
            c.commit()
            continue

        # Strip Arabic blocks — keep French body
        fr_txt = strip_arabic(raw_txt)
        print(f"  after Arabic strip: {len(fr_txt)} chars", flush=True)

        if len(fr_txt) < FLOOR:
            print(f"  Arabic-only PDF → skip", file=sys.stderr)
            skipped_arabic.append(cid)
            c.execute(
                "UPDATE beam_reports SET narrative_text=?, status='skipped', "
                "skip_reason='arabic-only', updated_at=? WHERE case_id=?",
                (raw_txt[:200], now(), cid),
            )
            c.commit()
            continue

        # Lookup known hint for event_date
        fn = urllib.parse.unquote(row["source_url"].split("/")[-1])
        known_hint = _KNOWN.get(fn) or _KNOWN.get(row["source_url"].split("/")[-1])
        known_reg = known_hint[0] if known_hint else None
        known_beam_ref = known_hint[1] if known_hint else None
        known_date = known_hint[2] if known_hint else None

        beam_ref = known_beam_ref or parse_beam_ref(fr_txt) or row["beam_ref"]
        registration = parse_registration_fr(fr_txt, known_reg or row["registration"])
        event_date = known_date or parse_date_fr(fr_txt)
        aircraft = parse_aircraft_fr(fr_txt)
        location = parse_location_fr(fr_txt)
        operator = parse_operator_fr(fr_txt)
        probable_cause = parse_probable_cause_fr(fr_txt)

        c.execute(
            """UPDATE beam_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 beam_ref=?, aircraft=?, location=?, operator=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (fr_txt, probable_cause, event_date, registration, beam_ref,
             aircraft, location, operator, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  date={event_date} reg={registration} beam_ref={beam_ref}", flush=True)

    if skipped_arabic:
        print(f"[beam parse] skipped Arabic-only: {skipped_arabic}", flush=True)
    print(f"[beam parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def build(c):
    """Write beam_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, beam_ref, registration, event_date, aircraft,
                  operator, location, narrative_text, probable_cause,
                  source_url, lang
           FROM beam_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE beam_reports SET status='skipped', skip_reason='no-text', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        cid = r["case_id"]
        # If beam_ref was found in text, update case_id accordingly
        if r["beam_ref"] and cid != "beam-" + r["beam_ref"].lower():
            new_cid = "beam-" + r["beam_ref"].lower()
            # Check if new_cid already exists
            existing = c.execute(
                "SELECT case_id FROM beam_accidents WHERE case_id=?", (new_cid,)
            ).fetchone()
            if not existing:
                cid = new_cid

        slug = cid.lower()

        c.execute(
            """INSERT OR REPLACE INTO beam_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'MA', ?, ?, ?, 'Rapport Final', ?, ?, ?)""",
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
                slug,
                r["lang"] or "fr",
                now(),
            ),
        )
        c.execute(
            "UPDATE beam_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[beam build] built={built}", flush=True)
    return built


# ---- RECHECK ----------------------------------------------------------------

def recheck(c, cl):
    rows = c.execute(
        "SELECT case_id, source_url FROM beam_reports "
        "WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    print(f"[beam recheck] checking {len(rows)} not-archived URLs", flush=True)
    newly = 0
    for row in rows:
        ts, _ = cdx_best_snapshot(cl, row["source_url"])
        if ts:
            print(f"  {row['case_id']} now has snapshot ts={ts}", flush=True)
            c.execute(
                "UPDATE beam_reports SET status='new', archive_ts=?, skip_reason=NULL, "
                "updated_at=? WHERE case_id=?",
                (ts, now(), row["case_id"]),
            )
            c.commit()
            newly += 1
    print(f"[beam recheck] newly_available={newly}", flush=True)
    return newly


# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- beam_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM beam_reports GROUP BY status, skip_reason"
    ):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):25s} {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM beam_accidents").fetchone()[0]
    print(f"\n--- beam_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM beam_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) FROM beam_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  narr_len min={narr[0]} max={narr[1]}")
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, LENGTH(narrative_text) len "
            "FROM beam_accidents ORDER BY event_date NULLS LAST LIMIT 15"
        ):
            print(f"    {r['case_id']:30s}  reg={r['registration'] or 'NULL':12s}  "
                  f"date={r['event_date'] or 'NULL'}  narr={r['len']}")

    skipped = c.execute(
        "SELECT case_id, skip_reason FROM beam_reports WHERE status='skipped'"
    ).fetchall()
    if skipped:
        print(f"\n  skipped ({len(skipped)}): {[(r['case_id'], r['skip_reason']) for r in skipped]}")


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
