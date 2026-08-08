#!/usr/bin/env python3
"""caavvu — Civil Aviation Authority of Vanuatu (CAAV) accident investigation ingest.

Source: https://caav.vu/investigation/  +  https://caav.vu/investigation/report-01/
  WordPress site with one completed investigation (as of 2026-06-11).

Report found:
  Report-01 (AA-2024-008 / CAAV investigation 136272/AIG/A2.5)
  Aircraft: Britten-Norman Islander BN2A-20, YJ-AT2
  Operator: Air Taxi Vanuatu
  Event:    Fuel starvation + terrain collision, 15 July 2024
  Location: 6 km ESE of Port Vila International Airport, Vanuatu
  Final:    https://caav.vu/wp-content/uploads/2025/08/CAAV-136272-AIG-A25-Final.pdf  (Aug 2025)
  Prelim:   https://caav.vu/wp-content/uploads/2024/09/CAAV-136272-AIG-A25-Preliminary-1.pdf (Sep 2024)
  Media:    https://caav.vu/wp-content/uploads/2024/09/240905-Media-Release-Summary.pdf
            https://caav.vu/wp-content/uploads/2025/08/CAAV-Investigation-136272-Final-Report-Media-Statement-2025.pdf

Supersession: keep FINAL, mark PRELIM as superseded.
Vanuatu registrations: YJ-XXX.
case_id: caavvu-<YYYYMMDD>-<reg-slug>  e.g. caavvu-20240715-yj-at2

Licence: Creative Commons Attribution 4.0 (CC-BY 4.0) per the report itself.

Stages: fetch | parse | build | all
Politeness: 2s delay. OCR via OCR_REMOTE if needed.
"""

import sys, os, re, time, sqlite3, subprocess, shlex, tempfile, uuid

DELAY    = 2.0
FLOOR    = 300
HOME     = os.path.expanduser("~/caavvu-ingest")
DB       = os.path.join(HOME, "caavvu.db")
PDFDIR   = os.path.join(HOME, "pdfs")
OCR_LANG = "eng"

BASE     = "https://caav.vu"
INDEX_URL = BASE + "/investigation/"

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

# ── KNOWN REPORTS ──────────────────────────────────────────────────────────────
# Prelim superseded by Final. Both are fetched; only Final goes to accidents.
KNOWN_REPORTS = [
    {
        "case_id":      "caavvu-20240715-yj-at2-prelim",
        "pdf_url":      BASE + "/wp-content/uploads/2024/09/CAAV-136272-AIG-A25-Preliminary-1.pdf",
        "pdf_file":     "caavvu-20240715-yj-at2-prelim.pdf",
        "event_date":   "2024-07-15",
        "registration": "YJ-AT2",
        "aircraft":     "Britten-Norman Islander BN2A-20",
        "operator":     "Air Taxi Vanuatu",
        "location":     "6 km east-south-east of Port Vila International Airport, Vanuatu",
        "country":      "VU",
        "report_type":  "Preliminary Report",
        "lang":         "en",
        "superseded_by":"caavvu-20240715-yj-at2",
    },
    {
        "case_id":      "caavvu-20240715-yj-at2",
        "pdf_url":      BASE + "/wp-content/uploads/2025/08/CAAV-136272-AIG-A25-Final.pdf",
        "pdf_file":     "caavvu-20240715-yj-at2-final.pdf",
        "event_date":   "2024-07-15",
        "registration": "YJ-AT2",
        "aircraft":     "Britten-Norman Islander BN2A-20",
        "operator":     "Air Taxi Vanuatu",
        "location":     "6 km east-south-east of Port Vila International Airport, Vanuatu",
        "country":      "VU",
        "report_type":  "Final Report",
        "lang":         "en",
        "superseded_by":None,
    },
]

# ── SCHEMA ──────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS caavvu_reports (
    case_id        TEXT PRIMARY KEY,
    pdf_url        TEXT,
    pdf_path       TEXT,
    event_date     TEXT,
    registration   TEXT,
    aircraft       TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'VU',
    report_type    TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    source_tier    TEXT,
    lang           TEXT DEFAULT 'en',
    superseded_by  TEXT,
    status         TEXT DEFAULT 'new',
    skip_reason    TEXT,
    discovered_at  INT,
    updated_at     INT
);
CREATE TABLE IF NOT EXISTS caavvu_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'VU',
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
CREATE INDEX IF NOT EXISTS idx_caavvu_status ON caavvu_reports(status);
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
    """Download PDFs for all reports."""
    import urllib.request
    os.makedirs(PDFDIR, exist_ok=True)

    for r in KNOWN_REPORTS:
        case_id  = r["case_id"]
        pdf_url  = r["pdf_url"]
        pdf_file = r["pdf_file"]
        dest     = os.path.join(PDFDIR, pdf_file)

        row = c.execute("SELECT status, pdf_path FROM caavvu_reports WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            ts = now()
            c.execute(
                "INSERT INTO caavvu_reports (case_id, pdf_url, event_date, registration, aircraft, "
                "operator, location, country, report_type, lang, superseded_by, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, pdf_url, r["event_date"], r["registration"], r["aircraft"],
                 r["operator"], r["location"], r["country"], r["report_type"],
                 r["lang"], r.get("superseded_by"), "new", ts, ts),
            )
            c.commit()
            row = c.execute("SELECT status, pdf_path FROM caavvu_reports WHERE case_id=?", (case_id,)).fetchone()

        if row["status"] not in ("new",):
            print(f"[caavvu fetch] {case_id}: skip (status={row['status']})")
            continue

        if os.path.exists(dest):
            print(f"[caavvu fetch] {case_id}: pdf already exists, advancing")
            c.execute("UPDATE caavvu_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            continue

        print(f"[caavvu fetch] {case_id}: downloading {pdf_url}")
        try:
            time.sleep(DELAY)
            req = urllib.request.Request(pdf_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
                fh.write(resp.read())
            c.execute("UPDATE caavvu_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), case_id))
            c.commit()
            print(f"[caavvu fetch] {case_id}: done ({os.path.getsize(dest)} bytes)")
        except Exception as e:
            print(f"[caavvu fetch] {case_id}: ERROR {e}", file=sys.stderr)


def do_parse(c):
    rows = c.execute("SELECT case_id, pdf_path, lang FROM caavvu_reports WHERE status='fetched'").fetchall()
    for row in rows:
        case_id  = row["case_id"]
        pdf_path = row["pdf_path"]

        text = extract_text(pdf_path)
        tier = "pdf"

        if len(text) < 300:
            print(f"[caavvu parse] {case_id}: short text ({len(text)}), trying OCR")
            text = ocr_extract(pdf_path, OCR_LANG)
            tier = "ocr" if len(text) >= 50 else "none"

        print(f"[caavvu parse] {case_id}: tier={tier} len={len(text)}")
        c.execute(
            "UPDATE caavvu_reports SET narrative_text=?, source_tier=?, status='parsed', updated_at=? WHERE case_id=?",
            (text, tier, now(), case_id),
        )
        c.commit()


def _extract_probable_cause(text):
    m = re.search(
        r'(?:PROBABLE\s+CAUSE|CONTRIBUTING\s+FACTORS?|SAFETY\s+ISSUES)[:\s]*\n(.*?)(?:\n\n|\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        cause = re.sub(r'\s+', ' ', m.group(1)).strip()
        if len(cause) > 30:
            return cause[:1000]
    return None


def do_build(c):
    rows = c.execute(
        "SELECT case_id, pdf_url, event_date, registration, aircraft, operator, location, "
        "country, report_type, narrative_text, source_tier, lang, superseded_by "
        "FROM caavvu_reports WHERE status='parsed'"
    ).fetchall()

    built = 0
    for row in rows:
        case_id   = row["case_id"]
        narrative = row["narrative_text"] or ""

        # Skip superseded (prelim)
        if row["superseded_by"]:
            print(f"[caavvu build] {case_id}: SKIP — superseded by {row['superseded_by']}")
            c.execute("UPDATE caavvu_reports SET status='superseded', updated_at=? WHERE case_id=?",
                      (now(), case_id))
            c.commit()
            continue

        if len(narrative) < FLOOR:
            reason = f"narrative too short ({len(narrative)} chars)"
            print(f"[caavvu build] {case_id}: SKIP — {reason}")
            c.execute("UPDATE caavvu_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                      (reason, now(), case_id))
            c.commit()
            continue

        probable_cause = _extract_probable_cause(narrative)
        slug = site_slug(row["aircraft"], row["registration"], row["location"])

        # Fatal count from the final report (1 passenger fatal per report text)
        fatalities_total = 1

        c.execute(
            "INSERT OR REPLACE INTO caavvu_accidents "
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
                fatalities_total,
                now(),
            ),
        )
        c.execute("UPDATE caavvu_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), case_id))
        c.commit()
        print(f"[caavvu build] {case_id}: built ({len(narrative)} chars, fatalities={fatalities_total})")
        built += 1

    print(f"[caavvu build] total built: {built}")
    return built


def verify(c):
    total = c.execute("SELECT COUNT(*) FROM caavvu_accidents").fetchone()[0]
    dated = c.execute("SELECT COUNT(*) FROM caavvu_accidents WHERE event_date IS NOT NULL").fetchone()[0]
    floor_ok = c.execute(f"SELECT COUNT(*) FROM caavvu_accidents WHERE length(narrative_text) >= {FLOOR}").fetchone()[0]
    dups = c.execute("SELECT COUNT(*)-COUNT(DISTINCT case_id) FROM caavvu_accidents").fetchone()[0]
    print(f"[caavvu verify] rows={total} dated={dated} floor={floor_ok} dups={dups}")
    rows = c.execute("SELECT case_id, event_date, length(narrative_text) len FROM caavvu_accidents ORDER BY event_date").fetchall()
    for r in rows:
        print(f"  {r['case_id']}  date={r['event_date']}  len={r['len']}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    c = conn()
    print(f"[caavvu] mode={mode}")
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
