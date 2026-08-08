#!/usr/bin/env python3
"""aacm — Civil Aviation Authority of Macao SAR aviation accident/incident ingest.

Source: www.aacm.gov.mo (Civil Aviation Authority of Macao SAR).
The live site is a Vue SPA with a WAF (CloudWAF) that blocks direct static file
access. The old site served PDFs at /images/download1/<name>.pdf, all confirmed
accessible via the Wayback Machine.

Reports seed: 4 confirmed accident/incident investigation reports with text
  layers. All retrieved from Wayback Machine archived snapshots.

Case id pattern: ACCID01/06, INCID01/05, INCID02/05, INCID01/2018
Offset: 121_000_000_000
Source code: aacm
CountryIso: MO
Lang: en
"""

import sys, os, sqlite3, subprocess, time, urllib.request, ssl
from pathlib import Path

HOME   = Path(os.path.expanduser("~/aacm-ingest"))
DB     = str(HOME / "aacm.db")
PDFDIR = HOME / "pdfs"
FLOOR  = 600   # min chars narrative for LLM fuel

COUNTRY = "MO"
LANG    = "en"
DELAY   = 3.0

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# ---------------------------------------------------------------------------
# Reports seed — 4 confirmed investigation reports via Wayback Machine CDX.
# Wayback timestamps verified 2026-06-22 (CDX filter=statuscode:200).
# Each entry: (case_id, original_filename, wayback_ts, event_date, aircraft,
#              registration, location, report_type, description)
# ---------------------------------------------------------------------------
REPORTS = [
    {
        "case_id": "aacm-ACCID01-06",
        "filename": "ACCID01_06InvestigationReport.pdf",
        "wayback_ts": "20220722103620",
        "event_date": "2006-03-04",
        "aircraft": "Airbus A321-231",
        "registration": "B-MAJ",
        "location": "Macau International Airport, Stand B4",
        "report_type": "Accident Report",
        "description": "Accident to Air Macau Airbus A321-231 B-MAJ during push back, Macau International Airport, 4 March 2006 (ACCID01/06)",
    },
    {
        "case_id": "aacm-INCID01-05",
        "filename": "AircraftIncidentReport01_05.pdf",
        "wayback_ts": "20220722103633",
        "event_date": "2005-09-16",
        "aircraft": "Boeing 747-45E / CRJ-700",
        "registration": "B-16402 / B-KBB",
        "location": "Macau International Airport",
        "report_type": "Incident Report",
        "description": "Ground incident between B747-400 (EVA Airways B-16402) and CRJ-700 (HK Express B-KBB), Macau International Airport, 16 September 2005 (INCID01/05)",
    },
    {
        "case_id": "aacm-INCID02-05",
        "filename": "201601072020742.pdf",
        "wayback_ts": "20220710013237",
        "event_date": "2005-01-01",
        "aircraft": "Unknown (Qatar Airways wet-lease)",
        "registration": None,
        "location": "Macau",
        "report_type": "Incident Report",
        "description": "Incident Final Report INCID 02/05 - QTR8505, Qatar Airways flight operated by CIELOS DEL PERU S.A.",
    },
    {
        "case_id": "aacm-INCID01-2018",
        "filename": "2019040315035642.pdf",
        "wayback_ts": "20221206114606",
        "event_date": "2018-08-28",
        "aircraft": "Airbus A320-214",
        "registration": "B6952",
        "location": "Macau International Airport",
        "report_type": "Incident Report",
        "description": "Aviation Occurrence Investigation Final Report INCID/01/2018 — Aircraft Damage Caused by Hard Landing, Beijing Capital Airlines Airbus A320-214 B6952, Macau International Airport, 28 August 2018",
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS aacm_reports (
    case_id            TEXT PRIMARY KEY,
    filename           TEXT,
    wayback_ts         TEXT,
    pdf_path           TEXT,
    event_date         TEXT,
    aircraft           TEXT,
    registration       TEXT,
    location           TEXT,
    report_type        TEXT,
    description        TEXT,
    narrative_text     TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    fetched_at         INTEGER,
    updated_at         INTEGER
);
CREATE TABLE IF NOT EXISTS aacm_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'MO',
    narrative_text TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    built_at       INTEGER
);
"""

WAYBACK = "https://web.archive.org/web"

# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def now_ms():
    return int(time.time() * 1000)

def make_ssl_ctx():
    ctx = ssl.create_default_context()
    return ctx

def fetch_url(url, dest, ctx):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
        data = resp.read()
    with open(dest, "wb") as fh:
        fh.write(data)
    return len(data)

def extract_text(pdf_path):
    try:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True, timeout=120
        )
        return result.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[aacm] pdftotext failed for {pdf_path}: {e}", file=sys.stderr)
        return ""

def make_site_slug(aircraft, registration, location):
    parts = []
    if aircraft:
        parts.append(aircraft)
    if registration:
        parts.append(registration)
    if location:
        parts.append(location)
    combined = "-".join(parts).lower()
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", combined).strip("-")[:80]
    return slug or "unknown"

# ---------------------------------------------------------------------------
# STAGES
# ---------------------------------------------------------------------------
def seed(conn):
    """Insert known reports (idempotent). Returns count inserted."""
    inserted = 0
    for r in REPORTS:
        if conn.execute("SELECT 1 FROM aacm_reports WHERE case_id=?", (r["case_id"],)).fetchone():
            continue
        ts = now_ms()
        conn.execute(
            "INSERT INTO aacm_reports "
            "(case_id, filename, wayback_ts, event_date, aircraft, registration, "
            "location, report_type, description, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], r["filename"], r["wayback_ts"],
             r["event_date"], r["aircraft"], r.get("registration"),
             r["location"], r["report_type"], r["description"],
             "new", ts)
        )
        inserted += 1
    conn.commit()
    return inserted

def fetch(conn, pdf_dir):
    """Download PDFs for status='new' rows from Wayback Machine."""
    pdf_dir = Path(pdf_dir)
    pdf_dir.mkdir(exist_ok=True)
    ctx = make_ssl_ctx()
    rows = conn.execute(
        "SELECT case_id, filename, wayback_ts FROM aacm_reports WHERE status='new'"
    ).fetchall()
    fetched = 0
    for row in rows:
        case_id = row["case_id"]
        filename = row["filename"]
        wayback_ts = row["wayback_ts"]
        original_url = f"https://www.aacm.gov.mo/images/download1/{filename}"
        wayback_url = f"{WAYBACK}/{wayback_ts}/{original_url}"
        dest = pdf_dir / f"{case_id}.pdf"
        print(f"[aacm fetch] {case_id}: {wayback_url}", file=sys.stderr)
        try:
            time.sleep(DELAY)
            size = fetch_url(wayback_url, dest, ctx)
            if size < 1000:
                print(f"[aacm fetch] {case_id}: suspiciously small ({size} bytes), skipping", file=sys.stderr)
                continue
            # Verify it's a PDF
            with open(dest, "rb") as f:
                header = f.read(5)
            if header != b"%PDF-":
                print(f"[aacm fetch] {case_id}: not a PDF (header={header}), skipping", file=sys.stderr)
                continue
            conn.execute(
                "UPDATE aacm_reports SET pdf_path=?, status='fetched', fetched_at=?, updated_at=? WHERE case_id=?",
                (str(dest), now_ms(), now_ms(), case_id)
            )
            conn.commit()
            fetched += 1
        except Exception as e:
            print(f"[aacm fetch] {case_id}: error: {e}", file=sys.stderr)
    return fetched

def parse(conn):
    """Extract text from fetched PDFs."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aacm_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        pdf_path = row["pdf_path"]
        txt = extract_text(pdf_path) if pdf_path else ""
        conn.execute(
            "UPDATE aacm_reports SET narrative_text=?, status='parsed', updated_at=? WHERE case_id=?",
            (txt, now_ms(), row["case_id"])
        )
        conn.commit()
        print(f"[aacm parse] {row['case_id']}: {len(txt)} chars", file=sys.stderr)
        parsed += 1
    return parsed

def build(conn):
    """Emit aacm_accidents rows from parsed rows with narrative >= FLOOR chars."""
    rows = conn.execute(
        "SELECT * FROM aacm_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < FLOOR:
            print(f"[aacm build] {row['case_id']}: narrative too short ({len(narrative)} chars), skipping", file=sys.stderr)
            conn.execute("UPDATE aacm_reports SET status='skipped', updated_at=? WHERE case_id=?", (now_ms(), row["case_id"]))
            conn.commit()
            continue
        site_slug = make_site_slug(row["aircraft"], row["registration"], row["location"])
        source_url = f"https://www.aacm.gov.mo/"
        conn.execute(
            "INSERT OR REPLACE INTO aacm_accidents "
            "(case_id, event_date, aircraft, registration, location, country, "
            "narrative_text, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["case_id"], row["event_date"], row["aircraft"],
             row["registration"], row["location"], COUNTRY,
             narrative, source_url, row["report_type"], site_slug, now_ms())
        )
        conn.execute("UPDATE aacm_reports SET status='built', updated_at=? WHERE case_id=?", (now_ms(), row["case_id"]))
        conn.commit()
        print(f"[aacm build] {row['case_id']}: built ({len(narrative)} chars)", file=sys.stderr)
        built += 1
    return built

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    PDFDIR.mkdir(exist_ok=True)
    conn = get_db()

    if stage in ("seed", "all"):
        n = seed(conn)
        print(f"[aacm] seeded {n} new report(s)", file=sys.stderr)

    if stage in ("fetch", "all"):
        n = fetch(conn, PDFDIR)
        print(f"[aacm] fetched {n} PDF(s)", file=sys.stderr)

    if stage in ("parse", "all"):
        n = parse(conn)
        print(f"[aacm] parsed {n} PDF(s)", file=sys.stderr)

    if stage in ("build", "all"):
        n = build(conn)
        print(f"[aacm] built {n} accident record(s)", file=sys.stderr)

    # Summary
    rows = conn.execute("SELECT COUNT(*) FROM aacm_accidents").fetchone()[0]
    print(f"[aacm] aacm_accidents total: {rows}", file=sys.stderr)
    rows_fuel = conn.execute("SELECT COUNT(*) FROM aacm_accidents WHERE length(narrative_text) >= ?", (FLOOR,)).fetchone()[0]
    print(f"[aacm] fuel (narrative>={FLOOR}): {rows_fuel}", file=sys.stderr)
    conn.close()
