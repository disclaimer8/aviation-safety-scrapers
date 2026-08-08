#!/usr/bin/env python3
"""bpea — Democratic Republic of Congo BPEA aviation-accident ingest.

Source: bpea.cd (Bureau Permanent d'Enquêtes d'Accidents et Incidents d'Aviation, DRC).
The live site is under maintenance (bpea.cd "site en travaux"). All PDFs fetched
from the Wayback Machine using specific archived timestamps where file size > 1.5MB
(truncated 1MB captures are broken/unreadable).

All reports are scanned-image PDFs (no text layer); OCR via ocrmypdf on hetzner
with --language fra (French is the official language of the DRC).

Reports seed: derived from CDX scan 2026-06-21. Only captures with confirmed
full-size files (non-truncated) are included.

Stages: fetch | parse | build | all  (via argv[1])

offset: 120_000_000_000
source code: bpea
countryIso: CD
lang: fr
"""

import sys, os, re, time, sqlite3, subprocess, shlex, uuid, json
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOME   = Path(os.path.expanduser("~/bpea-ingest"))
DB     = str(HOME / "bpea.db")
PDFDIR = HOME / "pdfs"
FLOOR  = 600   # min chars narrative

COUNTRY = "CD"
LANG    = "fr"
LIVE_BASE = "https://bpea.cd"
WAYBACK   = "https://web.archive.org/web"
OCR_LANG  = "fra"
DELAY     = 3.0  # seconds between Wayback fetches

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# ---------------------------------------------------------------------------
# REPORTS seed — derived from CDX scan 2026-06-21
# Each entry: (case_id, year, report_type, aircraft, wayback_ts, raw_filename, description)
# Only entries with confirmed full-size Wayback captures (>1.5MB CDX length)
# ---------------------------------------------------------------------------
REPORTS = [
    # D2-FIA Embraer 135 LR, 2023 incident at Kinshasa (final report)
    {
        "case_id": "bpea-D2-FIA-2023",
        "year": "2023",
        "report_type": "Final report",
        "aircraft": "Embraer 135 LR",
        "registration": "D2-FIA",
        "wayback_ts": "20240508192715",
        "raw_filename": "R.F%20EMBRAER%20135%20LR%20D2-FIA%20%202023.pdf",
        "description": "Incident of Embraer 135 LR D2-FIA, Trans Air Cargo Services, 03 March 2023, Aeroport International de N'djili, Kinshasa",
    },
    # 9S-GPK LET-410, 2022 accident (has text layer - no OCR needed)
    {
        "case_id": "bpea-9S-GPK-2022",
        "year": "2022",
        "report_type": "Final report",
        "aircraft": "LET 410",
        "registration": "9S-GPK",
        "wayback_ts": "20240902080044",
        "raw_filename": "Rapport%20d%27enquete%20_9S-GPK%20_%2003_11_2022.pdf",
        "description": "Accident of LET 410 9S-GPK, Goma Express, 03 November 2022, Aeroport International de N'djili/Kinshasa",
    },
    # 9S-ASG B737-300F, 04 March 2018 accident at Lubumbashi (prelim)
    {
        "case_id": "bpea-9S-ASG-2018-03-04-pre",
        "year": "2018",
        "report_type": "Preliminary report",
        "aircraft": "Boeing 737-300F",
        "registration": "9S-ASG",
        "wayback_ts": "20241203012622",
        "raw_filename": "RP%20737-300%20F%2004%20MARS%202018.pdf",
        "description": "Accident of Boeing 737-300F 9S-ASG, Lubumbashi (Luano), 04 March 2018 - Preliminary report (BPEA/ACCID 01/2018)",
    },
    # LET-410 UVP, 23 August 2014 (final report)
    {
        "case_id": "bpea-LET410-2014-08-23",
        "year": "2014",
        "report_type": "Final report",
        "aircraft": "LET 410 UVP",
        "registration": None,
        "wayback_ts": "20241203005837",
        "raw_filename": "RF%20LET410%20UVP%2023%20AOUT%202014.pdf",
        "description": "Accident of LET 410 UVP, 23 August 2014, DRC",
    },
    # B737-300, 03 March 2018 (circumstantial report / rapport circonstancié)
    {
        "case_id": "bpea-B737-2018-03-03",
        "year": "2018",
        "report_type": "Circumstantial report",
        "aircraft": "Boeing 737-300",
        "registration": None,
        "wayback_ts": "20241203005421",
        "raw_filename": "RC%20B737-300%2003%20Mars%202018.pdf",
        "description": "Circumstantial report (Rapport Circonstancié) of Boeing 737-300, 03 March 2018, DRC",
    },
    # B737-300, 04 May 2018 (circumstantial report)
    {
        "case_id": "bpea-B737-2018-05-04",
        "year": "2018",
        "report_type": "Circumstantial report",
        "aircraft": "Boeing 737-300",
        "registration": None,
        "wayback_ts": "20241203013644",
        "raw_filename": "RC%20B737-300%2004%20MAI%202018.pdf",
        "description": "Circumstantial report (Rapport Circonstancié) of Boeing 737-300, 04 May 2018, DRC",
    },
    # Cessna 172, 18 November 2020 (prelim report)
    {
        "case_id": "bpea-C172-2020-11-18",
        "year": "2020",
        "report_type": "Preliminary report",
        "aircraft": "Cessna 172",
        "registration": None,
        "wayback_ts": "20241203022102",
        "raw_filename": "RP%20CESSNA172%2018%20NOVEMBRE%202020.pdf",
        "description": "Preliminary report of Cessna 172, 18 November 2020, DRC",
    },
    # Be-200, 23 May 2018 (final report)
    {
        "case_id": "bpea-Be200-2018-05-23",
        "year": "2018",
        "report_type": "Final report",
        "aircraft": "Beriev Be-200",
        "registration": None,
        "wayback_ts": "20241203004557",
        "raw_filename": "RF%20Be-200%2023%20MAI%202018.pdf",
        "description": "Final report of Beriev Be-200, 23 May 2018, DRC",
    },
    # DHC-8-402, 14 August 2021 (final report)
    {
        "case_id": "bpea-DHC8-2021-08-14",
        "year": "2021",
        "report_type": "Final report",
        "aircraft": "DHC-8-402",
        "registration": None,
        "wayback_ts": "20241111080553",
        "raw_filename": "RF%20DHC8-402%2014%20AOUT%202021.pdf",
        "description": "Final report of DHC-8-402, 14 August 2021, DRC",
    },
    # AS 350 B3, 18 January 2016 (final report)
    {
        "case_id": "bpea-AS350-2016-01-18",
        "year": "2016",
        "report_type": "Final report",
        "aircraft": "AS 350 B3",
        "registration": None,
        "wayback_ts": "20241203005550",
        "raw_filename": "RF%20ECU.%20AS%20350%20B3%2018%20JANVIER%202016%20.pdf",
        "description": "Final report of AS 350 B3, 18 January 2016, DRC",
    },
]

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS bpea_reports (
  case_id      TEXT PRIMARY KEY,
  year         TEXT,
  report_type  TEXT,
  aircraft     TEXT,
  registration TEXT,
  description  TEXT,
  wayback_ts   TEXT,
  raw_filename TEXT,
  pdf_path     TEXT,
  narrative_text TEXT,
  source_tier  TEXT,
  event_date   TEXT,
  location     TEXT,
  lang         TEXT DEFAULT 'fr',
  status       TEXT DEFAULT 'new',
  discovered_at INTEGER,
  updated_at   INTEGER
);
CREATE TABLE IF NOT EXISTS bpea_accidents (
  case_id      TEXT PRIMARY KEY,
  event_date   TEXT,
  aircraft     TEXT,
  registration TEXT,
  operator     TEXT,
  location     TEXT,
  country      TEXT DEFAULT 'CD',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url   TEXT,
  report_type  TEXT,
  site_slug    TEXT,
  lang         TEXT DEFAULT 'fr',
  built_at     INTEGER
);
"""

def now_ms():
    return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

# ---------------------------------------------------------------------------
# OCR helpers (adapted from aaid-ingest pattern)
# ---------------------------------------------------------------------------
def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on hetzner via ssh/scp. Must run as a1."""
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
    """OCR a scanned PDF. Uses OCR_REMOTE env var for hetzner if set."""
    if not pdf_path or not Path(pdf_path).exists():
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    # Local fallback
    try:
        tmp = Path("/tmp") / ("ocr-%s.txt" % uuid.uuid4().hex)
        cp = subprocess.run(
            ["ocrmypdf", "--force-ocr", "--language", lang,
             "--sidecar", str(tmp), "--output-type", "none",
             str(pdf_path), "-"],
            capture_output=True, timeout=600,
        )
        if tmp.exists():
            t = tmp.read_text("utf-8", "replace").strip()
            tmp.unlink(missing_ok=True)
            return t
    except Exception:
        pass
    return ""


def extract_text(pdf_path):
    """Extract text via pdftotext. Returns empty string on failure."""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=60,
        )
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Stage: fetch
# ---------------------------------------------------------------------------
def fetch(c):
    """Download PDFs from Wayback Machine for new/unfetched reports."""
    import httpx
    rows = c.execute(
        "SELECT case_id, wayback_ts, raw_filename FROM bpea_reports WHERE status='new'"
    ).fetchall()
    fetched = 0
    client = httpx.Client(headers={"User-Agent": UA}, timeout=300.0, follow_redirects=True)
    PDFDIR.mkdir(parents=True, exist_ok=True)
    try:
        for row in rows:
            cid = row["case_id"]
            ts = row["wayback_ts"]
            fn = row["raw_filename"]
            pdf_path = PDFDIR / ("%s.pdf" % cid)

            wayback_url = "https://web.archive.org/web/%s/https://bpea.cd/assets/uploads/docbpea/rapenquetes/%s" % (ts, fn)
            print("[bpea fetch] %s -> %s" % (cid, wayback_url), flush=True)

            try:
                r = client.get(wayback_url)
                if r.status_code != 200:
                    print("[bpea fetch] HTTP %d for %s" % (r.status_code, cid), flush=True)
                    c.execute(
                        "UPDATE bpea_reports SET status='fetch_error', updated_at=? WHERE case_id=?",
                        (now_ms(), cid)
                    )
                    c.commit()
                    time.sleep(DELAY)
                    continue
                # Check size
                sz = len(r.content)
                if sz < 500_000:
                    print("[bpea fetch] file too small (%d bytes) for %s - skipping" % (sz, cid), flush=True)
                    c.execute(
                        "UPDATE bpea_reports SET status='fetch_error', updated_at=? WHERE case_id=?",
                        (now_ms(), cid)
                    )
                    c.commit()
                    time.sleep(DELAY)
                    continue
                pdf_path.write_bytes(r.content)
                print("[bpea fetch] OK %s (%d bytes)" % (cid, sz), flush=True)
                c.execute(
                    "UPDATE bpea_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (str(pdf_path), now_ms(), cid)
                )
                c.commit()
                fetched += 1
                time.sleep(DELAY)
            except Exception as e:
                print("[bpea fetch] error %s: %s" % (cid, e), flush=True)
                c.execute(
                    "UPDATE bpea_reports SET status='fetch_error', updated_at=? WHERE case_id=?",
                    (now_ms(), cid)
                )
                c.commit()
                time.sleep(DELAY)
    finally:
        client.close()
    return fetched


# ---------------------------------------------------------------------------
# Date/location extraction from French text
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(
    r'\b(\d{1,2})\s+(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})\b',
    re.I
)
_MONTH_MAP = {
    'janvier': '01', 'février': '02', 'fevrier': '02', 'mars': '03',
    'avril': '04', 'mai': '05', 'juin': '06', 'juillet': '07',
    'aout': '08', 'août': '08', 'septembre': '09', 'octobre': '10',
    'novembre': '11', 'décembre': '12', 'decembre': '12'
}

def extract_date(text):
    """Extract event date from French report text."""
    if not text:
        return None
    for m in _DATE_RE.finditer(text[:3000]):
        day, year = m.group(1), m.group(2)
        month_str = m.group(0).split()[1].lower()
        month = _MONTH_MAP.get(month_str)
        if month and int(year) > 1990:
            return "%s-%s-%02d" % (year, month, int(day))
    return None


def extract_location(text):
    """Extract location from French report text (simple heuristic)."""
    if not text:
        return None
    # Look for "à <place>" near "accident" or "incident"
    m = re.search(r'(?:accident|incident|survenu).*?(?:à|sur|près de)\s+([A-ZÀ-Ü][A-Za-zÀ-üé\- ]{3,40})', text[:2000], re.I)
    if m:
        loc = m.group(1).strip().rstrip('.,;')
        return loc[:80]
    return "Democratic Republic of Congo"


def site_slug(case_id, aircraft, registration):
    parts = []
    if aircraft:
        parts.append(re.sub(r'[^a-z0-9]+', '-', aircraft.lower()).strip('-'))
    if registration:
        parts.append(re.sub(r'[^a-z0-9]+', '-', registration.lower()).strip('-'))
    parts.append(re.sub(r'[^a-z0-9]+', '-', case_id.lower()).strip('-'))
    return '-'.join(p for p in parts if p)[:120]


# ---------------------------------------------------------------------------
# Stage: parse
# ---------------------------------------------------------------------------
def parse(c):
    """Parse fetched PDFs: pdftotext first, OCR fallback for scanned."""
    rows = c.execute(
        "SELECT case_id, pdf_path FROM bpea_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        if not pdf_path or not Path(pdf_path).exists():
            print("[bpea parse] PDF missing for %s" % cid, flush=True)
            continue

        # Try pdftotext first
        txt = extract_text(pdf_path)
        tier = "pdf"
        if len(txt) < FLOOR:
            # Try OCR
            print("[bpea parse] pdftotext short (%d chars), OCR-ing %s" % (len(txt), cid), flush=True)
            txt = ocr_extract(pdf_path, OCR_LANG)
            tier = "ocr"

        if len(txt) < FLOOR:
            print("[bpea parse] still below floor after OCR (%d chars) for %s" % (len(txt), cid), flush=True)
            c.execute(
                "UPDATE bpea_reports SET narrative_text=?, source_tier='none', status='skipped', updated_at=? WHERE case_id=?",
                ("", now_ms(), cid)
            )
            c.commit()
            continue

        # Extract date and location
        event_date = extract_date(txt)
        location = extract_location(txt)

        print("[bpea parse] OK %s: tier=%s chars=%d date=%s" % (cid, tier, len(txt), event_date), flush=True)
        c.execute(
            "UPDATE bpea_reports SET narrative_text=?, source_tier=?, event_date=?, location=?, status='parsed', updated_at=? WHERE case_id=?",
            (txt, tier, event_date, location, now_ms(), cid)
        )
        c.commit()
        parsed += 1
    return parsed


# ---------------------------------------------------------------------------
# Stage: build
# ---------------------------------------------------------------------------
def build(c):
    """Build bpea_accidents from parsed reports."""
    rows = c.execute(
        "SELECT case_id, year, report_type, aircraft, registration, description, "
        "pdf_path, narrative_text, source_tier, event_date, location, lang "
        "FROM bpea_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE bpea_reports SET status='skipped', updated_at=? WHERE case_id=?",
                (now_ms(), r["case_id"])
            )
            c.commit()
            continue

        # source_url = original bpea.cd URL (may be down, but is the canonical ref)
        source_url = "https://bpea.cd/assets/uploads/docbpea/rapenquetes/%s" % r["raw_filename"] if False else \
                     ("https://bpea.cd" if not r["pdf_path"] else
                      "https://bpea.cd/assets/uploads/docbpea/rapenquetes/")

        # Use case_id, aircraft, registration to build slug
        slug = site_slug(r["case_id"], r["aircraft"], r["registration"])

        c.execute(
            "INSERT OR REPLACE INTO bpea_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["case_id"],
                r["event_date"],
                r["aircraft"],
                r["registration"],
                None,
                r["location"] or "Democratic Republic of Congo",
                COUNTRY,
                narr,
                None,
                "https://bpea.cd",  # live site canonical (even if down)
                r["report_type"] or "Final report",
                slug,
                r["lang"] or LANG,
                now_ms(),
            )
        )
        c.execute(
            "UPDATE bpea_reports SET status='built', updated_at=? WHERE case_id=?",
            (now_ms(), r["case_id"])
        )
        c.commit()
        built += 1
    return built


# ---------------------------------------------------------------------------
# Seed: populate reports table from REPORTS list
# ---------------------------------------------------------------------------
def seed(c):
    """Insert REPORTS seed into bpea_reports (ignore existing)."""
    added = 0
    for rpt in REPORTS:
        try:
            c.execute(
                "INSERT OR IGNORE INTO bpea_reports "
                "(case_id, year, report_type, aircraft, registration, description, "
                "wayback_ts, raw_filename, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rpt["case_id"], rpt["year"], rpt["report_type"],
                    rpt["aircraft"], rpt.get("registration"),
                    rpt["description"], rpt["wayback_ts"], rpt["raw_filename"],
                    "new", now_ms(), now_ms(),
                )
            )
            if c.execute("SELECT changes()").fetchone()[0] > 0:
                added += 1
        except Exception as e:
            print("[bpea seed] error for %s: %s" % (rpt["case_id"], e), flush=True)
    c.commit()
    return added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    PDFDIR.mkdir(parents=True, exist_ok=True)
    c = conn()

    if mode in ("seed", "all"):
        n = seed(c)
        print("seeded: %d new" % n, flush=True)

    if mode in ("fetch", "all"):
        n = fetch(c)
        print("fetched: %d" % n, flush=True)

    if mode in ("parse", "all"):
        n = parse(c)
        print("parsed: %d" % n, flush=True)

    if mode in ("build", "all"):
        n = build(c)
        print("built: %d" % n, flush=True)

    # Status report
    rows = c.execute(
        "SELECT status, COUNT(*) as n FROM bpea_reports GROUP BY status ORDER BY n DESC"
    ).fetchall()
    print("reports:", [(r["status"], r["n"]) for r in rows], flush=True)
    n_acc = c.execute("SELECT COUNT(*) FROM bpea_accidents").fetchone()[0]
    print("accidents:", n_acc, flush=True)


if __name__ == "__main__":
    main()
