#!/usr/bin/env python3
"""aaibmm — Myanmar Aircraft Accident Investigation Bureau (AAIB / DCA Myanmar) ingest.

Source: https://dcamyanmar.com/dcadca/index.php?option=com_content&view=article&id=41
  Joomla page listing PDF report links under /dcadca/images/AAIB/.
  No post-2021 coup reports expected (DCA offline).

Report PDFs found (live on dcamyanmar.com as of 2026-06-11):
  1. AAIB/2.Final%20Report%20.pdf           — scanned image PDF (Adobe Acrobat 11 Image Conversion)
  2. AAIB/SERIOUS_INCIDENT_OF_..._XY-AMC_...pdf — 2016-11-16 Cessna 208B, Manaung
  3. AAIB/MNA%20Mawlamyine%20Final%20Report%20(English%20version).pdf — 2018-11-27 Cessna 208B, Mawlamyine
  4. AAIB/MNA_Emergency_landing_Final_report.pdf — 2019-05-12 Embraer-190, Mandalay
  5. AAIB/Final_Report_of_Golden_Myanmar_Airlines-ATR_72-600.pdf — 2019-08-02 ATR 72-600, Yangon
  (Additional via Wayback CDX):
  6. AAIB/Final-report-of-Embraer-190-XY-AGQ-emergency-landing.pdf — same as #4 (duplicate URL, same content)
  7. AAIB/Final_report_of_SINGAPORE_AIRLINES_9V-SSI.pdf — 2019-11-25 A330-343, Yangon

Note: 2.Final%20Report%20.pdf is a scanned image PDF (ocrmypdf may recover it).
      Final-report-of-Embraer-190-XY-AGQ-emergency-landing.pdf = duplicate of #4 (same content).

Myanmar registrations: XY-XXX.
case_id: aaibmm-<YYYYMMDD>-<reg-slug>
  e.g. aaibmm-20190512-xy-agq

Stages: fetch | parse | build | all   (via argv[1])
Politeness: 2s delay. OCR via OCR_REMOTE=<ocr-host> for scanned PDFs.
"""

import sys, os, re, time, sqlite3, subprocess, shlex, tempfile, uuid

DELAY    = 2.0
FLOOR    = 300
HOME     = os.path.expanduser("~/aaibmm-ingest")
DB       = os.path.join(HOME, "aaibmm.db")
PDFDIR   = os.path.join(HOME, "pdfs")
OCR_LANG = "eng"

BASE        = "https://dcamyanmar.com"
ARTICLE_URL = BASE + "/dcadca/index.php?option=com_content&view=article&id=41"
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE     = "https://web.archive.org/cdx/search/cdx"

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# ── KNOWN REPORTS ──────────────────────────────────────────────────────────────
# Hardcoded list (stable, small set). case_id = aaibmm-<YYYYMMDD>-<reg-slug>.
# Supersession: report #4 and Wayback extra #6 are the same XY-AGQ event;
# keep the larger/primary URL (MNA_Emergency_landing_Final_report.pdf = #4).
KNOWN_REPORTS = [
    {
        "case_id":     "aaibmm-20161116-xy-amc",
        "pdf_url":     BASE + "/dcadca/images/AAIB/SERIOUS_INCIDENT_OF_MYANMAR_NATIONAL_AIRLINES_CESSNA_GRAND_CARAVAN-Reg_XY-AMC-AT_MANAUNG_DOME.pdf",
        "pdf_file":    "aaibmm-20161116-xy-amc.pdf",
        "event_date":  "2016-11-16",
        "registration":"XY-AMC",
        "aircraft":    "Cessna 208B Grand Caravan",
        "operator":    "Myanmar National Airlines",
        "location":    "Manaung Domestic Airport, Rakhine State, Myanmar",
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
    {
        "case_id":     "aaibmm-20181127-xy-amb",
        "pdf_url":     BASE + "/dcadca/images/AAIB/MNA%20Mawlamyine%20Final%20Report%20(English%20version).pdf",
        "pdf_file":    "aaibmm-20181127-xy-amb.pdf",
        "event_date":  "2018-11-27",
        "registration":"XY-AMB",
        "aircraft":    "Cessna 208B Grand Caravan",
        "operator":    "Myanmar National Airlines",
        "location":    "Mawlamyine Domestic Airport, Mon State, Myanmar",
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
    {
        "case_id":     "aaibmm-20190512-xy-agq",
        "pdf_url":     BASE + "/dcadca/images/AAIB/MNA_Emergency_landing_Final_report.pdf",
        "pdf_file":    "aaibmm-20190512-xy-agq.pdf",
        "event_date":  "2019-05-12",
        "registration":"XY-AGQ",
        "aircraft":    "Embraer 190",
        "operator":    "Myanmar National Airlines",
        "location":    "Mandalay International Airport, Mandalay, Myanmar",
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
    {
        "case_id":     "aaibmm-20190802-xy-ajm",
        "pdf_url":     BASE + "/dcadca/images/AAIB/Final_Report_of_Golden_Myanmar_Airlines-ATR_72-600.pdf",
        "pdf_file":    "aaibmm-20190802-xy-ajm.pdf",
        "event_date":  "2019-08-02",
        "registration":"XY-AJM",
        "aircraft":    "ATR 72-600",
        "operator":    "Golden Myanmar Airlines",
        "location":    "Yangon International Airport, Yangon, Myanmar",
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
    {
        "case_id":     "aaibmm-20191125-9v-ssi",
        "pdf_url":     BASE + "/dcadca/images/AAIB/Final_report_of_SINGAPORE_AIRLINES_9V-SSI.pdf",
        "pdf_file":    "aaibmm-20191125-9v-ssi.pdf",
        "event_date":  "2019-11-25",
        "registration":"9V-SSI",
        "aircraft":    "Airbus A330-343",
        "operator":    "Singapore Airlines",
        "location":    "Yangon International Airport, Yangon, Myanmar",
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
    # 2.Final Report.pdf — scanned (image-only). Dates unknown from cover text alone.
    # Attempting OCR. Saved as special case_id with date unknown.
    {
        "case_id":     "aaibmm-scanned-2",
        "pdf_url":     BASE + "/dcadca/images/AAIB/2.Final%20Report%20.pdf",
        "pdf_file":    "aaibmm-scanned-2.pdf",
        "event_date":  None,
        "registration":None,
        "aircraft":    None,
        "operator":    None,
        "location":    None,
        "country":     "MM",
        "report_type": "Final Report",
        "lang":        "en",
    },
]

# ── SCHEMA ──────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS aaibmm_reports (
    case_id        TEXT PRIMARY KEY,
    pdf_url        TEXT,
    pdf_path       TEXT,
    event_date     TEXT,
    registration   TEXT,
    aircraft       TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'MM',
    report_type    TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    source_tier    TEXT,
    lang           TEXT DEFAULT 'en',
    status         TEXT DEFAULT 'new',
    skip_reason    TEXT,
    superseded_by  TEXT,
    discovered_at  INT,
    updated_at     INT
);
CREATE TABLE IF NOT EXISTS aaibmm_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'MM',
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
CREATE INDEX IF NOT EXISTS idx_aaibmm_status ON aaibmm_reports(status);
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


def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p]))
    return s.strip("-").lower()[:80] or None


# ── OCR ─────────────────────────────────────────────────────────────────────────
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


def extract_text(pdf_path):
    if not pdf_path:
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()


# ── STAGES ──────────────────────────────────────────────────────────────────────
def do_fetch(c):
    """Download PDFs for all new reports. Skip if pdf_path already exists."""
    import urllib.request
    os.makedirs(PDFDIR, exist_ok=True)

    for r in KNOWN_REPORTS:
        case_id  = r["case_id"]
        pdf_url  = r["pdf_url"]
        pdf_file = r["pdf_file"]
        dest     = os.path.join(PDFDIR, pdf_file)

        # Seed row if missing
        row = c.execute("SELECT status, pdf_path FROM aaibmm_reports WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            ts = now()
            c.execute(
                "INSERT INTO aaibmm_reports (case_id, pdf_url, event_date, registration, aircraft, "
                "operator, location, country, report_type, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, pdf_url, r["event_date"], r["registration"], r["aircraft"],
                 r["operator"], r["location"], r["country"], r["report_type"],
                 r["lang"], "new", ts, ts),
            )
            c.commit()
            row = c.execute("SELECT status, pdf_path FROM aaibmm_reports WHERE case_id=?", (case_id,)).fetchone()

        if row["status"] not in ("new",):
            print(f"[aaibmm fetch] {case_id}: skip (status={row['status']})")
            continue

        if os.path.exists(dest):
            print(f"[aaibmm fetch] {case_id}: pdf already exists, advancing to fetched")
            c.execute("UPDATE aaibmm_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            continue

        print(f"[aaibmm fetch] {case_id}: downloading {pdf_url}")
        try:
            time.sleep(DELAY)
            req = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
                fh.write(resp.read())
            c.execute("UPDATE aaibmm_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            print(f"[aaibmm fetch] {case_id}: done ({os.path.getsize(dest)} bytes)")
        except Exception as e:
            print(f"[aaibmm fetch] {case_id}: ERROR {e}", file=sys.stderr)


def do_parse(c):
    """Extract text from fetched PDFs. OCR scanned PDFs."""
    rows = c.execute("SELECT case_id, pdf_path, lang FROM aaibmm_reports WHERE status='fetched'").fetchall()
    for row in rows:
        case_id  = row["case_id"]
        pdf_path = row["pdf_path"]
        lang     = row["lang"] or "en"

        text = extract_text(pdf_path)
        tier = "pdf"

        if len(text) < 300:
            # Scanned — try OCR
            print(f"[aaibmm parse] {case_id}: text too short ({len(text)}), trying OCR")
            text = ocr_extract(pdf_path, OCR_LANG)
            tier = "ocr" if len(text) >= 50 else "none"

        print(f"[aaibmm parse] {case_id}: tier={tier} len={len(text)}")
        c.execute(
            "UPDATE aaibmm_reports SET narrative_text=?, source_tier=?, status='parsed', updated_at=? WHERE case_id=?",
            (text, tier, now(), case_id),
        )
        c.commit()


def _extract_probable_cause(text):
    """Try to find Probable Cause section in the narrative."""
    m = re.search(
        r'(?:PROBABLE\s+CAUSE|CAUSE\s+OF\s+(?:THE\s+)?(?:ACCIDENT|INCIDENT))[:\s]*\n(.*?)(?:\n\n|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        cause = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(cause) > 30:
            return cause[:1000]
    return None


def do_build(c):
    """Build aaibmm_accidents from parsed reports."""
    rows = c.execute(
        "SELECT case_id, pdf_url, event_date, registration, aircraft, operator, location, "
        "country, report_type, narrative_text, source_tier, lang "
        "FROM aaibmm_reports WHERE status='parsed'"
    ).fetchall()

    built = 0
    for row in rows:
        case_id   = row["case_id"]
        narrative = row["narrative_text"] or ""

        if len(narrative) < FLOOR:
            reason = f"narrative too short ({len(narrative)} chars)"
            print(f"[aaibmm build] {case_id}: SKIP — {reason}")
            c.execute("UPDATE aaibmm_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                      (reason, now(), case_id))
            c.commit()
            continue

        probable_cause = _extract_probable_cause(narrative)
        slug = site_slug(row["aircraft"], row["registration"], row["location"])

        c.execute(
            "INSERT OR REPLACE INTO aaibmm_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row["event_date"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                row["country"],
                narrative,
                probable_cause,
                row["pdf_url"],
                row["report_type"],
                slug,
                row["lang"] or "en",
                now(),
            ),
        )
        c.execute("UPDATE aaibmm_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), case_id))
        c.commit()
        print(f"[aaibmm build] {case_id}: built ({len(narrative)} chars)")
        built += 1

    print(f"[aaibmm build] total built: {built}")
    return built


def verify(c):
    """Print verification counts."""
    total = c.execute("SELECT COUNT(*) FROM aaibmm_accidents").fetchone()[0]
    dated = c.execute("SELECT COUNT(*) FROM aaibmm_accidents WHERE event_date IS NOT NULL").fetchone()[0]
    floor_ok = c.execute(f"SELECT COUNT(*) FROM aaibmm_accidents WHERE length(narrative_text) >= {FLOOR}").fetchone()[0]
    dups = c.execute("SELECT COUNT(*)-COUNT(DISTINCT case_id) FROM aaibmm_accidents").fetchone()[0]
    print(f"[aaibmm verify] rows={total} dated={dated} floor={floor_ok} dups={dups}")
    rows = c.execute("SELECT case_id, event_date, length(narrative_text) len FROM aaibmm_accidents ORDER BY event_date").fetchall()
    for r in rows:
        print(f"  {r['case_id']}  date={r['event_date']}  len={r['len']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = conn()
    print(f"[aaibmm] mode={mode}")
    if mode in ("fetch", "all"):
        do_fetch(c)
    if mode in ("parse", "all"):
        do_parse(c)
    if mode in ("build", "all"):
        do_build(c)
    if mode in ("verify", "all"):
        verify(c)
    c.close()


if __name__ == "__main__":
    main()
