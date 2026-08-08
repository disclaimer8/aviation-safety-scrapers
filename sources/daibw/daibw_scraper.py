#!/usr/bin/env python3
"""daibw — Botswana Directorate of Accident Investigation (DAI) ingest.

Source: https://gov.bw/ministries/directorate-accident-investigation
  Drupal portal (gov.bw). PDFs are served directly from /sites/default/files/.

Reports found (as of 2026-06-11):
  1. ZS-SBM_Report_Scanned.pdf
     Report No. MTC/AIG/01/21 — Fatal accident to Robinson R44 Raven II, ZS-SBM
     5 March 2021, Xumabee Game Ranch, Western Sand Veld near Sojwe, Botswana
     Scanned PDF (image-only) — OCR via OCR_REMOTE required.
     https://www.gov.bw/sites/default/files/2022-06/ZS-SBM_Report_Scanned.pdf

  A2-AJZ 2024 report: mentioned as forthcoming; NOT found on the live site
  or any Wayback snapshot as of 2026-06-11. Skipped.

Note: ZS-SBM is a South African registration (ZS = South Africa). The aircraft
  was operating in Botswana and the investigation was conducted by the Botswana DAI.
  country = BW (Botswana — the State of Occurrence and Investigating State).

Botswana registrations: A2-XXX.
case_id: daibw-<YYYYMMDD>-<reg-slug>  e.g. daibw-20210305-zs-sbm

Stages: fetch | parse | build | all
OCR: via OCR_REMOTE=<ocr-host> for scanned PDF.
"""

import sys, os, re, time, sqlite3, subprocess, shlex, tempfile, uuid

DELAY    = 2.0
FLOOR    = 300
HOME     = os.path.expanduser("~/daibw-ingest")
DB       = os.path.join(HOME, "daibw.db")
PDFDIR   = os.path.join(HOME, "pdfs")
OCR_LANG = "eng"

BASE     = "https://www.gov.bw"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# ── KNOWN REPORTS ──────────────────────────────────────────────────────────────
KNOWN_REPORTS = [
    {
        "case_id":       "daibw-20210305-zs-sbm",
        "pdf_url":       BASE + "/sites/default/files/2022-06/ZS-SBM_Report_Scanned.pdf",
        "pdf_file":      "daibw-zs-sbm.pdf",
        "event_date":    "2021-03-05",
        "registration":  "ZS-SBM",
        "aircraft":      "Robinson R44 Raven II",
        "operator":      None,
        "location":      "Xumabee Game Ranch, Western Sand Veld near Sojwe, Botswana",
        "country":       "BW",
        "report_type":   "Final Report",
        "report_number": "MTC/AIG/01/21",
        "lang":          "en",
        "scanned":       True,
    },
]

# ── SCHEMA ──────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS daibw_reports (
    case_id        TEXT PRIMARY KEY,
    report_number  TEXT,
    pdf_url        TEXT,
    pdf_path       TEXT,
    event_date     TEXT,
    registration   TEXT,
    aircraft       TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'BW',
    report_type    TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    source_tier    TEXT,
    lang           TEXT DEFAULT 'en',
    status         TEXT DEFAULT 'new',
    skip_reason    TEXT,
    discovered_at  INT,
    updated_at     INT
);
CREATE TABLE IF NOT EXISTS daibw_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'BW',
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
CREATE INDEX IF NOT EXISTS idx_daibw_status ON daibw_reports(status);
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
    import urllib.request
    os.makedirs(PDFDIR, exist_ok=True)

    for r in KNOWN_REPORTS:
        case_id  = r["case_id"]
        pdf_url  = r["pdf_url"]
        pdf_file = r["pdf_file"]
        dest     = os.path.join(PDFDIR, pdf_file)

        row = c.execute("SELECT status, pdf_path FROM daibw_reports WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            ts = now()
            c.execute(
                "INSERT INTO daibw_reports (case_id, report_number, pdf_url, event_date, registration, aircraft, "
                "operator, location, country, report_type, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, r.get("report_number"), pdf_url, r["event_date"], r["registration"], r["aircraft"],
                 r["operator"], r["location"], r["country"], r["report_type"],
                 r["lang"], "new", ts, ts),
            )
            c.commit()
            row = c.execute("SELECT status, pdf_path FROM daibw_reports WHERE case_id=?", (case_id,)).fetchone()

        if row["status"] not in ("new",):
            print(f"[daibw fetch] {case_id}: skip (status={row['status']})")
            continue

        if os.path.exists(dest):
            print(f"[daibw fetch] {case_id}: pdf already exists ({os.path.getsize(dest)} bytes), advancing")
            c.execute("UPDATE daibw_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            continue

        print(f"[daibw fetch] {case_id}: downloading {pdf_url}")
        try:
            time.sleep(DELAY)
            req = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
                fh.write(resp.read())
            c.execute("UPDATE daibw_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            print(f"[daibw fetch] {case_id}: done ({os.path.getsize(dest)} bytes)")
        except Exception as e:
            print(f"[daibw fetch] {case_id}: ERROR {e}", file=sys.stderr)


def do_parse(c):
    rows = c.execute("SELECT case_id, pdf_path FROM daibw_reports WHERE status='fetched'").fetchall()
    for row in rows:
        case_id  = row["case_id"]
        pdf_path = row["pdf_path"]

        text = extract_text(pdf_path)
        tier = "pdf"

        if len(text) < 300:
            # Scanned — try OCR
            print(f"[daibw parse] {case_id}: text too short ({len(text)}), trying OCR")
            text = ocr_extract(pdf_path, OCR_LANG)
            tier = "ocr" if len(text) >= 50 else "none"

        print(f"[daibw parse] {case_id}: tier={tier} len={len(text)}")
        c.execute(
            "UPDATE daibw_reports SET narrative_text=?, source_tier=?, status='parsed', updated_at=? WHERE case_id=?",
            (text, tier, now(), case_id),
        )
        c.commit()


def _extract_probable_cause(text):
    m = re.search(
        r'(?:PROBABLE\s+CAUSE|CAUSE\s+(?:OF\s+)?(?:THE\s+)?(?:ACCIDENT|INCIDENT)|CONTRIBUTING\s+FACTOR)[:\s]*\n(.*?)(?:\n\n|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        cause = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(cause) > 30:
            return cause[:1000]
    return None


def _extract_fatalities(text):
    """Try to extract fatality count from the narrative."""
    m = re.search(r'(\d+)\s+(?:persons?|people|passengers?|crew)\s+(?:on board\s+)?(?:were\s+)?(?:killed|fatal|died|lost\s+(?:their\s+)?lives)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Look for "X fatal" injuries pattern
    m = re.search(r'fatal\s*[:\s]+(\d+)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def do_build(c):
    rows = c.execute(
        "SELECT case_id, pdf_url, event_date, registration, aircraft, operator, location, "
        "country, report_type, narrative_text, source_tier, lang "
        "FROM daibw_reports WHERE status='parsed'"
    ).fetchall()

    built = 0
    for row in rows:
        case_id   = row["case_id"]
        narrative = row["narrative_text"] or ""

        if len(narrative) < FLOOR:
            reason = f"narrative too short ({len(narrative)} chars)"
            print(f"[daibw build] {case_id}: SKIP — {reason}")
            c.execute("UPDATE daibw_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                      (reason, now(), case_id))
            c.commit()
            continue

        probable_cause = _extract_probable_cause(narrative)
        fatalities = _extract_fatalities(narrative)
        slug = site_slug(row["aircraft"], row["registration"], row["location"])

        c.execute(
            "INSERT OR REPLACE INTO daibw_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, lang, "
            "fatalities_total, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                fatalities,
                now(),
            ),
        )
        c.execute("UPDATE daibw_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), case_id))
        c.commit()
        print(f"[daibw build] {case_id}: built ({len(narrative)} chars, fatalities={fatalities})")
        built += 1

    print(f"[daibw build] total built: {built}")
    return built


def verify(c):
    total = c.execute("SELECT COUNT(*) FROM daibw_accidents").fetchone()[0]
    dated = c.execute("SELECT COUNT(*) FROM daibw_accidents WHERE event_date IS NOT NULL").fetchone()[0]
    floor_ok = c.execute(f"SELECT COUNT(*) FROM daibw_accidents WHERE length(narrative_text) >= {FLOOR}").fetchone()[0]
    dups = c.execute("SELECT COUNT(*)-COUNT(DISTINCT case_id) FROM daibw_accidents").fetchone()[0]
    print(f"[daibw verify] rows={total} dated={dated} floor={floor_ok} dups={dups}")
    rows = c.execute("SELECT case_id, event_date, length(narrative_text) len, fatalities_total FROM daibw_accidents ORDER BY event_date").fetchall()
    for r in rows:
        print(f"  {r['case_id']}  date={r['event_date']}  len={r['len']}  fatalities={r['fatalities_total']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = conn()
    print(f"[daibw] mode={mode}")
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
