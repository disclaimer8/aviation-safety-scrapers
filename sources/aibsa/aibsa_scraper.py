#!/usr/bin/env python3
"""aibsa — Saudi Arabia Aviation Investigation Bureau (AIB) ingest.

Source: aib.gov.sa (www.aib.gov.sa/en-us/Pages/FinalReports.aspx)
Recovered entirely from the Wayback Machine.

DNS STATUS: aib.gov.sa has a lame delegation (GACA nameservers return SERVFAIL)
as of 2026-06-10. The live site uses Incapsula CDN. Do NOT probe the live site —
all PDFs are fetched from Wayback snapshots. Weekly re-check cycle must wait
until DNS is stable (Mac-only vantage limitation noted).

Reports found (Wayback CDX, 2026-06-10):
  FinalReports page: 2 investigation reports
    1. AIB-050822-996 (AIB-Report-Huraida.pdf) — Light Sport Aircraft Shoreline Crash,
       HZ-SAL, 5 Aug 2022, Asir Province, bilingual AR+EN, text PDF
    2. AIB-251021-1364 (AIB-251021-1364.pdf) — Hard Landing, B737-800 SU-GEE, EgyptAir
       MSR2677, 25 Oct 2021, Madinah, bilingual AR+EN, text PDF (4-page exec summary)

  Excluded (not individual accident investigation reports):
    - Annual Reports (AIB Annual Report 2015-2021, Annual report 2020-2021)
    - Quarterly Reports (2020 Q1, Q2)
    - Safety Studies (Bird Strikes Safety Study V1, Investigating Take-Off Performance)
    - View Eng.pdf / View Ar.pdf (Annual Safety Report 2021, PowerPoint)

case_id format: aibsa-<REPORT_NUMBER>
  e.g. aibsa-050822-996, aibsa-251021-1364

Stages: fetch | parse | build | all   (via argv[1])

PDFs are pre-placed in PDFDIR (downloaded on Mac where Wayback access is reliable
and SCP'd to minipc). Fetch stage uses Wayback if a PDF is missing.

Politeness: 2s delay between any HTTP requests.
OCR: via OCR_REMOTE=<ocr-host> if needed (eng only — ara not installed
     on hetzner; any Arabic-only PDF is skipped).
"""

import sys, os, re, time, sqlite3, subprocess, shlex, tempfile, uuid, json

DELAY        = 2.0
FLOOR        = 300       # narrative floor (chars) to count as live
HOME         = os.path.expanduser("~/aibsa-ingest")
DB           = os.path.join(HOME, "aibsa.db")
PDFDIR       = os.path.join(HOME, "pdfs")
OCR_LANG     = "eng"
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE     = "https://web.archive.org/cdx/search/cdx"
SOURCE_URL   = "https://www.aib.gov.sa/en-us/Pages/FinalReports.aspx"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# ---- KNOWN REPORTS (hardcoded — only 2 final reports in Wayback as of 2026-06-10) --------

KNOWN_REPORTS = [
    {
        "case_id":       "aibsa-050822-996",
        "report_number": "AIB-050822-996",
        "pdf_filename":  "AIB-Report-Huraida.pdf",
        "source_url":    "https://www.aib.gov.sa/en-us/Reports/AIB-Report-Huraida.pdf",
        "wayback_ts":    "20240303010857",
        "event_date":    "2022-08-05",
        "registration":  "HZ-SAL",
        "aircraft":      "Tecnam Astore P2002 (Light Sport Aircraft)",
        "location":      "Al-Huraidah Airstrip, Asir Province, Saudi Arabia",
        "country":       "SA",
        "report_type":   "Final Report",
        "lang":          "en",
    },
    {
        "case_id":       "aibsa-251021-1364",
        "report_number": "AIB-251021-1364",
        "pdf_filename":  "AIB-251021-1364.pdf",
        "source_url":    "https://www.aib.gov.sa/en-us/Reports/AIB-251021-1364.pdf",
        "wayback_ts":    "20240706134557",   # 4.4 MB complete capture
        "event_date":    "2021-10-25",
        "registration":  "SU-GEE",
        "aircraft":      "Boeing 737-800",
        "location":      "Prince Mohammad Bin Abdulaziz International Airport (OEMA), Madinah, Saudi Arabia",
        "country":       "SA",
        "report_type":   "Final Report",
        "lang":          "en",
    },
]

# ---- SCHEMA --------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS aibsa_reports (
  case_id        TEXT PRIMARY KEY,
  report_number  TEXT,
  source_url     TEXT,
  wayback_ts     TEXT,
  pdf_path       TEXT,
  pdf_filename   TEXT,
  event_date     TEXT,
  registration   TEXT,
  aircraft       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'SA',
  report_type    TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  source_tier    TEXT,   -- 'pdf', 'ocr', 'none'
  lang           TEXT DEFAULT 'en',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS aibsa_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'SA',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'en',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_aibsa_status ON aibsa_reports(status);
"""

# ---- HELPERS -------------------------------------------------------------------

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


def _wayback_url(ts, original_url):
    return f"{WAYBACK_BASE}/{ts}/{original_url}"


def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p]))
    return s.strip("-").lower()[:80] or None


# ---- OCR / TEXT EXTRACTION -------------------------------------------------------

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
    """OCR a PDF. Uses OCR_REMOTE env var if set (hetzner), else local ocrmypdf."""
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


def extract_text(path):
    """Extract text from PDF using pdftotext."""
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                              capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


# Arabic script Unicode blocks
_ARABIC_RE = re.compile(
    r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+"
    r"[\s؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]*",
    re.UNICODE,
)


def strip_arabic(text):
    """Remove Arabic-script blocks from text, keep Latin/English body."""
    cleaned = _ARABIC_RE.sub(" ", text)
    cleaned = re.sub(r"\s{3,}", "\n\n", cleaned)
    return cleaned.strip()


def is_usable_text(text, min_ascii_chars=FLOOR):
    """Return True if text has enough ASCII/Latin content."""
    ascii_chars = sum(1 for c in text if ord(c) < 128 and c.isprintable() and not c.isspace())
    return ascii_chars >= min_ascii_chars


# ---- PARSE HELPERS -------------------------------------------------------------

def parse_narrative_en(text):
    """Extract English narrative body from bilingual AR+EN PDF text.

    Strategy: pdftotext on these bilingual AIB reports yields a mix of Arabic and
    English. We strip Arabic blocks, keeping the English executive summary / report
    body. The English portion is substantial (300-3000+ chars in these reports).
    """
    if not text:
        return ""
    stripped = strip_arabic(text)
    # Remove excessive whitespace/pagination artefacts
    cleaned = re.sub(r"\n{3,}", "\n\n", stripped)
    cleaned = re.sub(r"[ \t]{4,}", " ", cleaned)
    return cleaned.strip()


def parse_probable_cause(text):
    """Extract probable cause / conclusion from English report text."""
    if not text:
        return None
    # Look for Conclusion, Probable Cause, Investigation revealed sections
    for pat in [
        r"(?:Conclusion|Probable Cause)[s]?\s*\n(.*?)(?:\n\n|\Z)",
        r"investigation revealed\s+(.*?)(?:\n\n|\Z)",
        r"cause.*?(?:was|were)\s+(.*?)(?:\.|$)",
    ]:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            v = m.group(1).strip()
            if 20 < len(v) < 2000:
                return v
    return None


def parse_operator(text):
    """Extract operator name from report text."""
    if not text:
        return None
    for pat in [
        r"(?:Operator|Airline|Company)[:\s]+([A-Za-z][^\n]{5,60})",
        r"Egypt Air|Saudi Arabian Airlines|Saudi Aramco|TARCO",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
            v = v.split("\n")[0].strip().strip(",;.")
            if 3 < len(v) < 80:
                return v
    return None


# ---- FETCH -------------------------------------------------------------------

def fetch(c, cl=None):
    """Download any missing PDFs from Wayback. Skips if already on disk."""
    rows = c.execute(
        "SELECT case_id, pdf_filename, source_url, wayback_ts "
        "FROM aibsa_reports WHERE status='new'"
    ).fetchall()
    print(f"[aibsa fetch] {len(rows)} reports to fetch", flush=True)

    # Import httpx inline to avoid dependency if not needed
    if cl is None:
        try:
            import httpx
            cl = httpx.Client(headers={"User-Agent": UA}, timeout=120.0, follow_redirects=True)
            own_cl = True
        except ImportError:
            import urllib.request
            cl = None
            own_cl = False
    else:
        own_cl = False

    fetched = 0
    for row in rows:
        cid = row["case_id"]
        pdf_filename = row["pdf_filename"]
        pdf_path = os.path.join(PDFDIR, pdf_filename)

        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 10000:
            print(f"  [skip] {cid} — PDF already on disk ({os.path.getsize(pdf_path)} bytes)", flush=True)
            c.execute(
                "UPDATE aibsa_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (pdf_path, now(), cid),
            )
            c.commit()
            fetched += 1
            continue

        wurl = _wayback_url(row["wayback_ts"], row["source_url"])
        print(f"  [fetch] {cid} from {wurl}", flush=True)
        try:
            if cl:
                r = cl.get(wurl)
                time.sleep(DELAY)
                if r.status_code != 200:
                    print(f"  HTTP {r.status_code}", file=sys.stderr)
                    c.execute(
                        "UPDATE aibsa_reports SET status='skipped', skip_reason='http-error', "
                        "updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue
                data = r.content
            else:
                req = urllib.request.Request(wurl, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                time.sleep(DELAY)

            with open(pdf_path, "wb") as fh:
                fh.write(data)
            print(f"  saved {len(data)} bytes to {pdf_path}", flush=True)
            c.execute(
                "UPDATE aibsa_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (pdf_path, now(), cid),
            )
            c.commit()
            fetched += 1
        except Exception as e:
            print(f"  [error] {cid}: {e}", file=sys.stderr)
            c.execute(
                "UPDATE aibsa_reports SET status='skipped', skip_reason='fetch-error', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()

    if own_cl and cl:
        cl.close()
    print(f"[aibsa fetch] fetched={fetched}", flush=True)
    return fetched


# ---- PARSE -------------------------------------------------------------------

def parse(c):
    """Extract text and metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, registration, aircraft, location, event_date, report_type "
        "FROM aibsa_reports WHERE status='fetched'"
    ).fetchall()
    print(f"[aibsa parse] {len(rows)} reports to parse", flush=True)
    parsed = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"  [parse] {cid} {pdf_path}", flush=True)

        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  PDF missing at {pdf_path}", file=sys.stderr)
            c.execute(
                "UPDATE aibsa_reports SET status='skipped', skip_reason='pdf-missing', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        # Try pdftotext first
        raw_text = extract_text(pdf_path)
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"  pdftotext → {len(raw_text)} chars raw (file={size_kb}KB)", flush=True)

        source_tier = "pdf"

        if not is_usable_text(raw_text):
            print(f"  insufficient Latin text ({len(raw_text)} chars) → trying OCR", flush=True)
            ocr_text = ocr_extract(pdf_path, OCR_LANG)
            print(f"  OCR → {len(ocr_text)} chars", flush=True)
            if is_usable_text(ocr_text):
                raw_text = ocr_text
                source_tier = "ocr"
            else:
                # Both failed — check if at least the description from KNOWN_REPORTS
                # can serve as a narrative (the Arabic PDF with no English content)
                print(f"  both pdftotext and OCR insufficient → skipped", file=sys.stderr)
                c.execute(
                    "UPDATE aibsa_reports SET status='skipped', skip_reason='no-text', "
                    "source_tier='none', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

        # Strip Arabic to keep English body
        narrative = parse_narrative_en(raw_text)
        print(f"  after Arabic strip: {len(narrative)} chars", flush=True)

        if len(narrative) < FLOOR:
            print(f"  narrative too short ({len(narrative)} < {FLOOR}) → skipped", file=sys.stderr)
            c.execute(
                "UPDATE aibsa_reports SET status='skipped', skip_reason='short-narrative', "
                "source_tier=?, updated_at=? WHERE case_id=?",
                (source_tier, now(), cid),
            )
            c.commit()
            continue

        probable_cause = parse_probable_cause(narrative)
        operator = parse_operator(narrative)

        c.execute(
            """UPDATE aibsa_reports SET
                 narrative_text=?, probable_cause=?, source_tier=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (narrative, probable_cause, source_tier, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  cause={probable_cause[:80] if probable_cause else None}", flush=True)

    print(f"[aibsa parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def build(c):
    """Write aibsa_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, report_number, registration, event_date, aircraft,
                  location, country, narrative_text, probable_cause,
                  source_url, report_type, lang
           FROM aibsa_reports WHERE status='parsed'"""
    ).fetchall()
    print(f"[aibsa build] {len(rows)} to build", flush=True)
    built = 0

    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            continue

        slug = site_slug("aibsa", r["registration"] or r["report_number"] or r["case_id"])

        c.execute(
            """INSERT OR REPLACE INTO aibsa_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["case_id"],
                r["event_date"],
                r["aircraft"],
                r["registration"],
                None,   # operator (could be parsed later if needed)
                r["location"],
                r["country"],
                narr,
                r["probable_cause"],
                r["source_url"],
                r["report_type"],
                slug,
                r["lang"],
                now(),
            ),
        )
        c.execute(
            "UPDATE aibsa_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1
        print(f"  built {r['case_id']} narr={len(narr)}", flush=True)

    print(f"[aibsa build] built={built}", flush=True)
    return built


# ---- STATS -------------------------------------------------------------------

def print_stats(c):
    print("\n--- aibsa_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, COUNT(*) n FROM aibsa_reports GROUP BY status, skip_reason"
    ):
        print(f"  {row[0]:10s} {(row[1] or ''):25s} {row[2]}")

    cnt = c.execute("SELECT COUNT(*) FROM aibsa_accidents").fetchone()[0]
    print(f"\n--- aibsa_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM aibsa_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) FROM aibsa_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  narr_len min={narr[0]} max={narr[1]}")
        print("\n  rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, LENGTH(narrative_text) len, source_tier "
            "FROM aibsa_reports WHERE status='built' ORDER BY event_date"
        ):
            print(f"    {r[0]:35s}  reg={r[1] or 'NULL':10s}  "
                  f"date={r[2] or 'NULL'}  narr={r[3]}  tier={r[4]}")

    skipped = c.execute(
        "SELECT case_id, skip_reason FROM aibsa_reports WHERE status='skipped'"
    ).fetchall()
    if skipped:
        print(f"\n  skipped ({len(skipped)}): {[(r[0], r[1]) for r in skipped]}")


# ---- INIT -------------------------------------------------------------------

def init_reports(c):
    """Seed the reports table from KNOWN_REPORTS if not already present."""
    print(f"[aibsa init] seeding {len(KNOWN_REPORTS)} known reports", flush=True)
    for rep in KNOWN_REPORTS:
        existing = c.execute(
            "SELECT case_id FROM aibsa_reports WHERE case_id=?", (rep["case_id"],)
        ).fetchone()
        if existing:
            print(f"  already exists: {rep['case_id']}", flush=True)
            continue
        c.execute(
            """INSERT INTO aibsa_reports
               (case_id, report_number, source_url, wayback_ts, pdf_filename,
                event_date, registration, aircraft, location, country,
                report_type, lang, status, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (
                rep["case_id"], rep["report_number"], rep["source_url"],
                rep["wayback_ts"], rep["pdf_filename"],
                rep["event_date"], rep["registration"], rep["aircraft"],
                rep["location"], rep["country"], rep["report_type"],
                rep["lang"], now(), now(),
            ),
        )
        print(f"  inserted: {rep['case_id']}", flush=True)
    c.commit()


# ---- MAIN -------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()
    init_reports(c)

    if mode in ("fetch", "all"):
        try:
            import httpx
            cl = httpx.Client(headers={"User-Agent": UA}, timeout=120.0, follow_redirects=True)
            try:
                fetch(c, cl)
            finally:
                cl.close()
        except ImportError:
            # Fallback: urllib (no httpx)
            fetch(c, None)

    if mode in ("parse", "all"):
        parse(c)

    if mode in ("build", "all"):
        build(c)

    print_stats(c)


if __name__ == "__main__":
    main()
