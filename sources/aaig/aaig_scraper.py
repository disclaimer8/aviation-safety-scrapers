#!/usr/bin/env python3
"""AAIG (Bangladesh — Aircraft Accident Investigation Group, under CAAB) ingest.

Source: caab.gov.bd/aaig/ accident-report PDFs — recovered ENTIRELY from the
Wayback Machine because live caab.gov.bd serves dead links (301 → homepage)
after a site redesign.

DO NOT probe caab.gov.bd directly.

Report PDF URL pattern:
  http://www.caab.gov.bd/aaig/<filename>.pdf
  Filenames follow no strict convention; mostly S2-XXX registrations or
  descriptive names. All are catalogued directly from CDX API.

case_id format: aaig-<reg_slug>
  e.g. aaig-s2-adi, aaig-s2-agz, aaig-b777-vghs-2023
  Prefix 'aaig-' is MANDATORY — bare reg IDs are not globally unique.

Stages: discover | fetch | parse | build | recheck | all  (via argv[1])

Politeness: 2 s base delay, exponential backoff on 429/503 (30–60 s, 3 retries).
"""
import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, urllib.request

CAAB_AAIG_BASE = "http://www.caab.gov.bd/aaig"

WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

DELAY = 2.0
FLOOR = 80
HOME = os.path.expanduser("~/aaig-ingest")
DB = os.path.join(HOME, "aaig.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaig_reports (
  case_id      TEXT PRIMARY KEY,
  source_url   TEXT,        -- original caab.gov.bd PDF URL
  archive_url  TEXT,        -- Wayback snapshot URL used
  archive_ts   TEXT,        -- CDX timestamp of snapshot
  pdf_path     TEXT,
  filename     TEXT,        -- basename of original URL
  report_type  TEXT,
  aircraft     TEXT,
  registration TEXT,
  event_date   TEXT,
  location     TEXT,
  operator     TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang         TEXT DEFAULT 'en',
  status       TEXT DEFAULT 'new',
  skip_reason  TEXT,
  discovered_at INT,
  updated_at   INT
);
CREATE TABLE IF NOT EXISTS aaig_accidents (
  case_id       TEXT PRIMARY KEY,
  event_date    TEXT,
  aircraft      TEXT,
  registration  TEXT,
  operator      TEXT,
  location      TEXT,
  country       TEXT DEFAULT 'BD',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url    TEXT,
  report_type   TEXT,
  site_slug     TEXT,
  lang          TEXT DEFAULT 'en',
  built_at      INT
);
CREATE INDEX IF NOT EXISTS idx_aaig_status ON aaig_reports(status);
"""

# Accident-report PDF candidates discovered via CDX:
# These are specifically accident/incident investigation reports, not ICAO manuals,
# legislation, or other regulatory docs. Chosen by: S2-reg in filename, or
# explicitly named FINAL/PRELIMINARY/INTERIM REPORT, or clearly named incidents.
# Duplicates (same accident, multiple filename variants) are deduplicated by case_id.
# The "preferred" URL is chosen: .pdf extension > no extension; 'Final' > 'interim'.
CDX_REPORTS = [
    # (original_url, best_cdx_ts, label)
    # S2-ADI — earliest archived (2018)
    ("http://www.caab.gov.bd:80/aaig/S2-ADI.pdf", "20180328175917", "S2-ADI"),
    # S2-AHW — 2019
    ("http://www.caab.gov.bd:80/aaig/S2-AHW.pdf", "20190712134708", "S2-AHW"),
    # S2-AFP — 2021
    ("http://www.caab.gov.bd/aaig/S2-AFP.pdf", "20210517233311", "S2-AFP"),
    # S2-AAX Final — prefer Final over bare/DF variants
    ("http://www.caab.gov.bd/aaig/S2-AAX%20Final.pdf", "20220704070401", "S2-AAX Final"),
    # S2-AGU Final — prefer Final
    ("http://www.caab.gov.bd/aaig/S2-AGU%20Final.pdf", "20220704070406", "S2-AGU Final"),
    # AN-26B S2-AGZ — 2017
    ("http://www.caab.gov.bd:80/aaig/AN-26B%20%20S2-AGZ.pdf", "20170517060122", "AN-26B S2-AGZ"),
    # S2-AGQ
    ("http://www.caab.gov.bd/aaig/S2-AGQ.pdf", "20220704070439", "S2-AGQ"),
    # S2-AGT
    ("http://www.caab.gov.bd/aaig/S2-AGT.pdf", "20220704070502", "S2-AGT"),
    # S2-ADQ Final — prefer FINAL REPORT version
    ("http://www.caab.gov.bd/aaig/FINAL%20REPORT%20S2-ADQ.pdf", "20220704070530", "FINAL REPORT S2-ADQ"),
    # ATR 72-500 — likely a Biman Bangladesh ATR72 accident
    ("http://www.caab.gov.bd/aaig/ATR%2072-500.pdf", "20220704070556", "ATR 72-500"),
    # S2-AHI Final — prefer Final
    ("http://www.caab.gov.bd/aaig/S2-AHI%20Final.pdf", "20240519174634", "S2-AHI Final"),
    # HS-TJD Final — Thai-registered, investigated by Bangladesh (foreign aircraft accident in BD)
    ("http://www.caab.gov.bd/aaig/HS-TJD%20Final.pdf", "20220118050850", "HS-TJD Final"),
    # S2-AIB
    ("http://www.caab.gov.bd/aaig/S2-AIB.pdf", "20220702052716", "S2-AIB"),
    # S2-AFK Final (prefer over interim/bare)
    ("http://www.caab.gov.bd/aaig/S2AFKFinal.pdf", "20230601200857", "S2-AFK Final"),
    # S2-AHF Final
    ("http://www.caab.gov.bd/aaig/S2AHFFinal.pdf", "20240519173658", "S2-AHF Final"),
    # S2-AJA — B737-800 (prefer latest version)
    ("http://www.caab.gov.bd/aaig/boeing%20737%20800%20S2%20AJA.pdf", "20240519175317", "B737-800 S2-AJA"),
    # S2-AJD
    ("http://www.caab.gov.bd/aaig/S2-AJD.pdf", "20240610173202", "S2-AJD"),
    # S2-AGG (Cessna 152) — Final version preferred
    ("http://www.caab.gov.bd/aaig/S2-AGG.pdf", "20220118051128", "S2-AGG Cessna152"),
    # B-777 VGHS runway excursion 2023 (Saudia SV-806) — Final report
    ("http://www.caab.gov.bd/aaig/FINAL%20REPORT%20OF%20RUNWAY%20EXCURSION%20OF%20B-777%20AIRCRAFT%20AT%20VGHS%20OCCURRED%20ON%2028%20JUNE%202023.pdf",
     "20250420045225", "B-777 VGHS 2023 Final"),
    # DHC-8 Q-400 S2-AJW — Preliminary (no final yet)
    ("http://www.caab.gov.bd/aaig/PRELIMINARY%20REPORT%20OF%20DHC-8%20Q-400%20S2-AJW.pdf",
     "20251026161939", "DHC-8 Q-400 S2-AJW Preliminary"),
    # S2-AAX DF (Data Factual — separate document, different content from Final)
    ("http://www.caab.gov.bd/aaig/S2-AAX%20DF.pdf", "20250611205814", "S2-AAX Data Factual"),
]

# These are deliberately excluded (ICAO doc series, legislation, contact pages):
# - 10206_cons_en.pdf = ICAO Convention 10206
# - aaic2023.pdf = AAIC annual report (not an accident investigation report)
# - aigorder.pdf = organizational order
# - Aircraft Accident and Serious Incident Investigation Rules 2023
# - ANNEX 13, Airport Services Manual, Convention on International Civil Aviation
# - Hazards at Aircraft Accident Sites, Human Factors Training Manual, ICAO Policy docs
# - Manual of Aircraft Accident and Incident Investigation (ICAO DOC 9756 parts)
# - Manual of Civil Aviation Medicine, Manual on Assistance, Manual on Regional...
# - Safety Management Manual, CA Act, CAA Act
# - historyaaic.pdf, aaic2023.pdf
# - INTERIM STATEMENT S2-AFK (superseded by Final)
# - PRELIMINARY REPORT Saudia SV-806 (superseded by Final)
# - S2-AHI.pdf (superseded by S2-AHI Final.pdf)
# - S2-AJA.pdf, S2AJA.pdf, REG S2-AJA.pdf (superseded by boeing 737 800 S2 AJA.pdf)
# - CESSNA152 S2-AGG (superseded by S2-AGG.pdf)
# - S2-ADQ.pdf (superseded by FINAL REPORT S2-ADQ.pdf)
# - S2-AFKFinal no hyphen = same as S2AFKFinal
# - S2-AGU no ext, S2-AAX no ext = no-extension dupes

EN_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_EN_MONTH_ALT = "|".join(sorted(EN_MONTHS, key=len, reverse=True))
_EN_DATE_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\.?\s+(" + _EN_MONTH_ALT + r")[,.\s]+(\d{4})\b",
    re.IGNORECASE,
)
_EN_DATE_MDY = re.compile(
    r"\b(" + _EN_MONTH_ALT + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_ISO = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[.\-/]([01]?\d)[.\-/]((?:19|20)\d{2})\b"
)
_REG_RE = re.compile(r"\b(S2-[A-Z]{2,3}|HS-[A-Z]{3}|[A-Z]{1,2}-[A-Z0-9]{2,5})\b")


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
    delay = DELAY
    for attempt in range(retries):
        try:
            r = cl.get(url)
            if r.status_code in (429, 503):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] {r.status_code} -> sleep {wait}s", flush=True)
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


def _en_month_num(m):
    k = m.lower()
    if k in EN_MONTHS:
        return EN_MONTHS[k]
    for key in sorted(EN_MONTHS, key=len, reverse=True):
        if k.startswith(key):
            return EN_MONTHS[key]
    return None


def parse_date(txt):
    if not txt:
        return None
    header = txt[:2500]
    m = _EN_DATE_DMY.search(header)
    if m:
        d, ms, y = int(m.group(1)), m.group(2), int(m.group(3))
        mo = _en_month_num(ms)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _EN_DATE_MDY.search(header)
    if m:
        ms, d, y = m.group(1), int(m.group(2)), int(m.group(3))
        mo = _en_month_num(ms)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _DATE_ISO.search(header)
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
    # Prefer S2- Bangladesh registrations
    m = re.search(r"\bS2-[A-Z]{2,3}\b", txt[:4000])
    if m:
        return m.group(0)
    m = _REG_RE.search(txt[:4000])
    return m.group(1) if m else None


def parse_aircraft(txt):
    if not txt:
        return None
    header = txt[:4000]
    for pat in [
        r"Aircraft\s+[Tt]ype[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"Type\s+of\s+[Aa]ircraft[:\s]+([A-Za-z0-9][\w\s\-/]{2,40})",
        r"aircraft[,\s]+([A-Za-z][\w\s\-/]{2,35}),?\s+registration",
        r"\btype[:\s]+([A-Za-z0-9][\w\s\-/]{2,35})[,\n]",
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
        r"[Ll]ocation[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"[Pp]lace\s+of\s+[Oo]ccurrence[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"[Oo]ccurrence\s+[Ss]ite[:\s]+([A-Za-z][\w\s\-,/]{3,60})",
        r"near\s+([A-Z][a-z]{2,}[\w\s,]{0,40}),?\s+[Bb]angladesh",
        r"[Aa]t\s+([A-Za-z][\w\s\-,]{3,50})\s+[Aa]irport",
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
        r"[Oo]perator[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"[Oo]wner[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
        r"[Oo]wner[/\\][Oo]perator[:\s]+([A-Za-z][\w\s\-,./]{3,60})",
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
        r"(?:PROBABLE\s+CAUSE|PROBABLE CAUSE)[\s:]*\n(.*?)(?=\n[A-Z]{4}|\Z)",
        r"(?:CONCLUSIONS?|FINDING[S]?\s+OF\s+CAUSE)[\s:]*\n(.*?)(?=\n[A-Z]{4}|\Z)",
        r"(?:Probable cause[s]?|Conclusions?)[\s:]*\n(.*?)(?=\n[A-Z]{4}|\Z)",
        r"(?:CAUSES?\s+OF\s+THE\s+ACCIDENT)[\s:]*\n(.*?)(?=\n[A-Z]{4}|\Z)",
    ]:
        m = re.search(header_pat, txt, re.DOTALL | re.IGNORECASE)
        if m:
            section = m.group(1).strip()
            section = re.split(r"\n(?:[A-Z][A-Z\s]{5,}|[0-9]+\.)\s*\n", section)[0]
            section = section[:2000].strip()
            if len(section) > 30:
                return section
    return None


def extract_text(path):
    """Extract text from PDF via pdftotext, or from OCR sidecar if available."""
    if not path or not os.path.exists(path):
        return ""
    # Check for OCR sidecar (.ocr.txt) produced by remote OCR step
    sidecar = path.replace(".pdf", ".ocr.txt")
    if os.path.exists(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                txt = fh.read().strip()
            if len(txt) >= FLOOR:
                return txt
        except OSError:
            pass
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(path), "-"],
            capture_output=True, timeout=180,
        )
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


def case_id_from_url(orig_url):
    """Derive stable case_id from original URL. Prefix 'aaig-' mandatory."""
    fn = urllib.parse.unquote(orig_url.rstrip("/").split("/")[-1])
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I).strip()

    # S2-XXX registration in filename -> aaig-s2-xxx
    # Handle: S2-AFK, S2AFK, S2AFKFinal, "S2 AJA", "AN-26B S2-AGZ"
    # Try strict word-boundary first (S2-AFK, S2 AJA), then prefix match (S2AFKFinal)
    m = re.search(r"\bS2[-\s]?([A-Z]{2,3})\b", fn, re.IGNORECASE)
    if not m:
        # S2XXXFinal style: S2 followed by exactly 3 uppercase letters then non-letter
        m = re.search(r"\bS2([A-Z]{3})(?=[A-Z][a-z]|\d|$)", fn, re.IGNORECASE)
    if m:
        return "aaig-s2-" + m.group(1).lower()

    # HS-TJD (Thai reg in BD-investigated accident)
    m = re.search(r"\bHS-([A-Z]{3})\b", fn, re.IGNORECASE)
    if m:
        return "aaig-hs-" + m.group(1).lower()

    # Named reports: B-777 VGHS 2023
    if "B-777" in fn.upper() or "VGHS" in fn.upper():
        return "aaig-b777-vghs-2023"

    # DHC-8
    if "DHC-8" in fn.upper() or "Q-400" in fn.upper():
        # extract reg from filename if present
        m2 = re.search(r"\bS2-([A-Z]+)\b", fn.upper())
        if m2:
            return "aaig-s2-" + m2.group(1).lower()
        return "aaig-dhc8-q400-2024"

    # ATR 72
    if "ATR" in fn.upper():
        return "aaig-atr72-biman"

    # Fallback: slugify the filename
    slug = re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-").lower()
    return "aaig-" + slug[:40]


def report_type_from_url(orig_url):
    fn = urllib.parse.unquote(orig_url).lower()
    if "final" in fn:
        return "Final report"
    if "preliminary" in fn:
        return "Preliminary report"
    if "interim" in fn:
        return "Interim report"
    if "data" in fn or " df" in fn:
        return "Data factual"
    return "Final report"


def cdx_best_snapshot(cl, orig_url):
    cdx_url = (
        f"{CDX_BASE}?url={urllib.parse.quote(orig_url, safe=':/')}"
        f"&output=json&filter=statuscode:200&limit=5"
    )
    try:
        r = wayback_get(cl, cdx_url)
        if not r or r.status_code != 200:
            return None
        rows = r.json()
        if len(rows) <= 1:
            return None
        return rows[-1][1]
    except Exception as e:
        print(f"  [cdx] error for {orig_url}: {e}", file=sys.stderr)
        return None


# ---- DISCOVER ----------------------------------------------------------------

def discover(c, cl):
    """Populate aaig_reports from hardcoded CDX_REPORTS list.

    The 'listing page' approach is not applicable here because caab.gov.bd's
    AAIG section has no stable machine-readable index — PDFs were enumerated
    directly from the CDX API (cdx?url=caab.gov.bd/aaig/*).
    The CDX_REPORTS list is the output of that recon phase.
    """
    print(f"[aaig discover] loading {len(CDX_REPORTS)} candidates from CDX recon", flush=True)
    inserted = 0
    for orig_url, cdx_ts, label in CDX_REPORTS:
        cid = case_id_from_url(orig_url)
        if not cid:
            print(f"  [warn] no case_id for {orig_url}", file=sys.stderr)
            continue
        existing = c.execute("SELECT case_id FROM aaig_reports WHERE case_id=?", (cid,)).fetchone()
        if existing:
            print(f"  [discover] already known: {cid}", flush=True)
            continue
        fn = urllib.parse.unquote(orig_url.rstrip("/").split("/")[-1])
        rtype = report_type_from_url(orig_url)
        c.execute(
            "INSERT OR IGNORE INTO aaig_reports "
            "(case_id, source_url, archive_ts, filename, report_type, lang, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'en', 'new', ?, ?)",
            (cid, orig_url, cdx_ts, fn, rtype, now(), now()),
        )
        c.commit()
        inserted += 1
        print(f"  [discover] {cid} ({label})", flush=True)
    print(f"[aaig discover] inserted {inserted} new rows", flush=True)
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url, archive_ts FROM aaig_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0
    not_archived = []

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        # Use CDX-known timestamp directly (pre-verified during recon)
        ts = row["archive_ts"]
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[aaig fetch] {cid} ...", flush=True)

        # Already downloaded?
        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            if ts:
                c.execute(
                    "UPDATE aaig_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
                downloaded += 1
                print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
                continue

        if not ts:
            # Try CDX lookup as fallback
            ts = cdx_best_snapshot(cl, orig_url)
        if not ts:
            print(f"  [aaig fetch] {cid}: not archived", flush=True)
            not_archived.append(cid)
            c.execute(
                "UPDATE aaig_reports SET status='skipped', skip_reason='not-archived', updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
        print(f"  ts={ts} url=...{orig_url[-50:]}", flush=True)

        try:
            r = wayback_get(cl, archive_url)
            if not r or r.status_code != 200:
                print(f"  HTTP {r.status_code if r else 'none'} for {cid}", file=sys.stderr)
                c.execute(
                    "UPDATE aaig_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (f"http-{r.status_code if r else 'none'}", now(), cid),
                )
                c.commit()
                continue

            if r.content[:4] != b"%PDF":
                print(f"  [aaig fetch] {cid}: not a PDF (got {r.content[:20]!r})", file=sys.stderr)
                c.execute(
                    "UPDATE aaig_reports SET status='skipped', skip_reason='not-pdf', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

            with open(dest, "wb") as fh:
                fh.write(r.content)
            c.execute(
                "UPDATE aaig_reports SET pdf_path=?, archive_url=?, archive_ts=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, archive_url, ts, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved {len(r.content)//1024}KB", flush=True)

        except Exception as e:
            print(f"  [aaig fetch] {cid}: {e}", file=sys.stderr)
            c.execute(
                "UPDATE aaig_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                (str(e)[:120], now(), cid),
            )
            c.commit()

    print(f"[aaig fetch] downloaded={downloaded} not_archived={len(not_archived)}", flush=True)
    if not_archived:
        print(f"  not-archived: {not_archived}", flush=True)
    return downloaded, not_archived


# ---- PARSE -------------------------------------------------------------------

def parse(c):
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, report_type, lang FROM aaig_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[aaig parse] {cid}", flush=True)

        txt = extract_text(pdf_path)
        if len(txt) < FLOOR:
            print(f"  {cid}: only {len(txt)} chars -> OCR-TODO or skip", flush=True)
            c.execute(
                "UPDATE aaig_reports SET narrative_text=?, status='skipped', skip_reason='no-text-ocr-todo', updated_at=? WHERE case_id=?",
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

        print(f"  date={event_date} reg={registration} loc={location} chars={len(txt)}", flush=True)

        c.execute(
            """UPDATE aaig_reports SET
                 narrative_text=?, probable_cause=?, event_date=?, registration=?,
                 aircraft=?, location=?, operator=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, event_date, registration, aircraft, location, operator, now(), cid),
        )
        c.commit()
        parsed += 1
    print(f"[aaig parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def build(c):
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, operator, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM aaig_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE aaig_reports SET status='skipped', skip_reason='no-text', updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        cid = r["case_id"]   # already has 'aaig-' prefix from discover
        slug = cid.lower()   # site_slug = cid (e.g. 'aaig-s2-adi')

        operator = r["operator"] or parse_operator(narr)

        c.execute(
            """INSERT OR REPLACE INTO aaig_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'BD', ?, ?, ?, ?, ?, ?, ?)""",
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
            "UPDATE aaig_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1
    print(f"[aaig build] built={built}", flush=True)
    return built


# ---- RECHECK -----------------------------------------------------------------

def recheck(c, cl):
    rows = c.execute(
        "SELECT case_id, source_url FROM aaig_reports WHERE status='skipped' AND skip_reason='not-archived'"
    ).fetchall()
    print(f"[aaig recheck] checking {len(rows)} not-archived URLs", flush=True)
    newly = 0
    for row in rows:
        ts = cdx_best_snapshot(cl, row["source_url"])
        if ts:
            print(f"  [recheck] {row['case_id']} now has ts={ts}", flush=True)
            c.execute(
                "UPDATE aaig_reports SET status='new', archive_ts=?, skip_reason=NULL, updated_at=? WHERE case_id=?",
                (ts, now(), row["case_id"]),
            )
            c.commit()
            newly += 1
    print(f"[aaig recheck] newly_available={newly}", flush=True)
    return newly


# ---- STATS -------------------------------------------------------------------

def print_stats(c):
    print("\n--- status counts ---")
    for row in c.execute("SELECT status, skip_reason, count(*) n FROM aaig_reports GROUP BY status, skip_reason"):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):25s} {row['n']}")
    cnt = c.execute("SELECT COUNT(*) FROM aaig_accidents").fetchone()[0]
    print(f"\n--- aaig_accidents: {cnt} rows ---")
    if cnt:
        row = c.execute(
            "SELECT SUM(lang='en'), MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) FROM aaig_accidents"
        ).fetchone()
        print(f"  lang en={row[0]}  narr_len min={row[1]} max={row[2]}")
        null_dates = c.execute("SELECT COUNT(*) FROM aaig_accidents WHERE event_date IS NULL").fetchone()[0]
        print(f"  event_date NULL: {null_dates}")
        print("\n  SELECT case_id, event_date, registration, site_slug, LENGTH(narrative_text):")
        for r in c.execute(
            "SELECT case_id, event_date, registration, site_slug, LENGTH(narrative_text) len "
            "FROM aaig_accidents ORDER BY case_id"
        ):
            print(f"    {r['case_id']:30s}  date={str(r['event_date'] or 'NULL'):12s}  reg={str(r['registration'] or 'NULL'):10s}  narr={r['len']}")
    na = c.execute(
        "SELECT case_id, skip_reason FROM aaig_reports WHERE status='skipped' ORDER BY case_id"
    ).fetchall()
    if na:
        print(f"\n  skipped ({len(na)}):")
        for r in na:
            print(f"    {r['case_id']}  reason={r['skip_reason']}")


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
            print(f"recheck: {newly} newly available")
            if newly:
                print("Run 'fetch' then 'parse' then 'build' to ingest them.")
        finally:
            cl.close()

    print_stats(c)


if __name__ == "__main__":
    main()
