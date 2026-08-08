#!/usr/bin/env python3
"""beaci — Bureau d'Enquêtes Accidents d'Aviation de Côte d'Ivoire (BEA-CI)
aviation-accident ingest.  country CI, lang 'fr'.

Listing: https://bea.ci/140-2/ ("RAPPORTS D'ENQUETES")
Extra PDF found via WP media API (not on listing): TU-GAF

Known reports (10 unique PDFs, 9 distinct accidents after dedup):
  From listing (9 hrefs, but TU-TRM appears twice with same size — deduped → 8 unique):
    Rapport-final-enquete-ACCID-AL-II-TU-HAI-mai2020.pdf     TU-HAI  2020
    Rapport-final-ACCID-TU-GAD-oct-2020-1.pdf                TU-GAD  2020
    Rapport-final-ACCID-TU-HAH-oct19-1.pdf                   TU-HAH  2019
    RAPPORT-FINAL-ER-AVB-BEA-CI-mars2019-1.pdf               ER-AVB  2019
    RAPPORT-FINALPdf-TU-TGY.pdf                              TU-TGY  ?
    Rapport-Final-Pdf-Rapport-ACCID-TU-TRM-20oct14-1.pdf     TU-TRM  2014 (canonical)
    Rapport-Final-Pdf-OD-MEA-A330-21-aout-2011.pdf           OD-MEA  2011
    Rapport-Final-Pdf-KQ-A310-300-5Y-BEN-30janv2000-1.pdf    5Y-BEN  2000
  From WP media API (not on listing):
    Rapport-final-Pdf-ACCID-TU-GAF-avril17.pdf               TU-GAF  2017

case_id = 'beaci-' + registration.lower().replace(' ', '-')
          If registration not parseable: 'beaci-' + slug derived from filename.

event_date: extracted from French date patterns in PDF text:
  "le 20 octobre 2014", "le 30 janvier 2000", "21 août 2011", etc.
  Fallback: date hint from filename (e.g. "20oct14" -> 2014-10-20).

OCR: if pdftotext yields < FLOOR chars, run remote OCR on hetzner with lang fra.
     OCR_REMOTE env must be set to '<ocr-host>'.
     Run as a1 on minipc (not root).

Stages: discover | fetch | parse | build | all
"""

import sys, os, re, time, sqlite3, subprocess, json, uuid, shutil

SOURCE   = "beaci"
COUNTRY  = "CI"
LANG     = "fr"
HOME     = os.path.expanduser(f"~/beaci-ingest")
DB       = os.path.join(HOME, "beaci.db")
PDFDIR   = os.path.join(HOME, "pdfs")
FLOOR    = 300   # narrative floor
UA       = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DELAY    = 2.0

LISTING_URL = "https://bea.ci/140-2/"

# All known PDF URLs (listing + WP media extra).
# TU-TRM appears twice on listing with identical content-length — keep only -1 variant.
KNOWN_PDFS = [
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-final-enquete-ACCID-AL-II-TU-HAI-mai2020.pdf",
     "TU-HAI", "2020"),
    ("https://bea.ci/wp-content/uploads/2020/11/Rapport-final-ACCID-TU-GAD-oct-2020-1.pdf",
     "TU-GAD", "2020"),
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-final-ACCID-TU-HAH-oct19-1.pdf",
     "TU-HAH", "2019"),
    ("https://bea.ci/wp-content/uploads/2020/06/RAPPORT-FINAL-ER-AVB-BEA-CI-mars2019-1.pdf",
     "ER-AVB", "2019"),
    ("https://bea.ci/wp-content/uploads/2020/06/RAPPORT-FINALPdf-TU-TGY.pdf",
     "TU-TGY", None),
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-Final-Pdf-Rapport-ACCID-TU-TRM-20oct14-1.pdf",
     "TU-TRM", "2014"),
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-Final-Pdf-OD-MEA-A330-21-aout-2011.pdf",
     "OD-MEA", "2011"),
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-Final-Pdf-KQ-A310-300-5Y-BEN-30janv2000-1.pdf",
     "5Y-BEN", "2000"),
    # Extra from WP media API (not on listing)
    ("https://bea.ci/wp-content/uploads/2020/06/Rapport-final-Pdf-ACCID-TU-GAF-avril17.pdf",
     "TU-GAF", "2017"),
]

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
# ISO or numeric dates
_DATE_YMD = re.compile(r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b")
_DATE_DMY = re.compile(r"\b([0-3]?\d)[./\-]([01]\d)[./\-]((?:19|20)\d{2})\b")

# Ivorian registration patterns: TU-XXX  (ICAO prefix TU)
# Also foreign: ER-AVB (Moldova), OD-MEA (Lebanon), 5Y-BEN (Kenya), etc.
_REG_PATTERNS = re.compile(
    r"\b(TU-[A-Z]{3}|ER-[A-Z]{3}|OD-[A-Z]{3}|5Y-[A-Z]{3}|[A-Z]{1,2}-[A-Z]{3,4}|[A-Z]\d{3,5}[A-Z]{0,2}|N\d{3,5}[A-Z]{0,2})\b"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS beaci_reports (
  case_id        TEXT PRIMARY KEY,
  source_url     TEXT,
  pdf_path       TEXT,
  registration   TEXT,
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang           TEXT DEFAULT 'fr',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS beaci_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'CI',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT DEFAULT 'Rapport Final',
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  fatalities_total INT,
  phase          TEXT,
  category       TEXT,
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_beaci_status ON beaci_reports(status);
"""


def now_ms():
    return int(time.time() * 1000)


def get_conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def get_client():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=15.0,
        follow_redirects=True,
    )


def case_id_for(registration):
    """Return canonical case_id string."""
    return SOURCE + "-" + registration.lower().replace(" ", "-")


def _case_id_from_filename(url):
    """Fallback case_id from URL filename slug."""
    fn = url.split("/")[-1]
    fn = re.sub(r'\.pdf$', '', fn, flags=re.I)
    fn = re.sub(r'[^a-zA-Z0-9]+', '-', fn).lower().strip('-')
    return SOURCE + "-" + fn[:60]


def extract_date_fr(text):
    """Extract earliest plausible occurrence date from French text."""
    candidates = []
    for m in _FR_MONTH_PAT.finditer(text):
        day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
        month_no = _FR_MONTHS.get(month_name)
        if month_no and 1950 <= int(year) <= 2030:
            candidates.append(f"{year}-{month_no:02d}-{int(day):02d}")
    for m in _DATE_YMD.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1950 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
            candidates.append(f"{y}-{mo:02d}-{d:02d}")
    for m in _DATE_DMY.finditer(text):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1950 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
            candidates.append(f"{y}-{mo:02d}-{d:02d}")
    return candidates[0] if candidates else None


def extract_date_hint_from_filename(url):
    """Extract partial date from filename (e.g. '20oct14', 'mars2019')."""
    fn = url.split("/")[-1].lower()
    # Pattern: DDmonYY or DDmonYYYY in filename
    m = re.search(r'(\d{1,2})(jan|fev|mar|avr|mai|juin|juil|aou|sep|oct|nov|dec)(\d{2,4})', fn)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)
        yr = int(m.group(3))
        if yr < 100:
            yr += 2000
        month_map = {"jan":1,"fev":2,"mar":3,"avr":4,"mai":5,"juin":6,
                     "juil":7,"aou":8,"sep":9,"oct":10,"nov":11,"dec":12}
        mo = month_map.get(mon_str, 0)
        if mo and 1950 <= yr <= 2030:
            return f"{yr}-{mo:02d}-{day:02d}"
    # Pattern: MonYYYY (e.g. mars2019, avr17)
    m = re.search(r'(jan|fev|mars|avr|mai|juin|juil|aout|sep|oct|nov|dec)(\d{2,4})', fn)
    if m:
        mon_str = m.group(1)
        yr = int(m.group(2))
        if yr < 100:
            yr += 2000
        month_map = {"jan":1,"fev":2,"mars":3,"avr":4,"mai":5,"juin":6,
                     "juil":7,"aout":8,"sep":9,"oct":10,"nov":11,"dec":12}
        mo = month_map.get(mon_str, 0)
        if mo and 1950 <= yr <= 2030:
            return f"{yr}-{mo:02d}-01"
    return None


def extract_registration(text):
    """Find first aircraft registration in text."""
    for m in _REG_PATTERNS.finditer(text):
        reg = m.group(1)
        # Skip common false positives
        if reg in ("A-330", "B-737"):
            continue
        return reg
    return None


def pdftotext(pdf_path):
    """Extract text via pdftotext. Returns string (may be empty)."""
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", pdf_path, "-"],
            capture_output=True, timeout=60
        )
        return r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"  [pdftotext] {e}", file=sys.stderr)
        return ""


def ocr_remote(pdf_path, lang="fra"):
    """OCR a scanned PDF via OCR_REMOTE hetzner host. Returns text or ''."""
    host = os.environ.get("OCR_REMOTE", "")
    if not host:
        print(f"  [ocr] OCR_REMOTE not set, skipping OCR for {pdf_path}", file=sys.stderr)
        return ""
    remote_tmp = f"/tmp/ocr-{uuid.uuid4().hex}.pdf"
    out_tmp = pdf_path + ".ocr"
    try:
        subprocess.run(["scp", "-q", pdf_path, f"{host}:{remote_tmp}"],
                       timeout=120, check=True)
        cmd = (
            f"nice -n 10 ionice -c 3 ocrmypdf --rotate-pages --deskew "
            f"--language {lang} --force-ocr "
            f"{remote_tmp} {remote_tmp}.out.pdf 2>/dev/null && "
            f"pdftotext -layout -enc UTF-8 {remote_tmp}.out.pdf - ; "
            f"rm -f {remote_tmp} {remote_tmp}.out.pdf"
        )
        r = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=300)
        text = r.stdout.decode("utf-8", "replace").strip()
        return text
    except Exception as e:
        print(f"  [ocr_remote] {e}", file=sys.stderr)
        return ""


# ── STAGES ────────────────────────────────────────────────────────────────────

def discover(conn):
    """Insert known PDF entries into beaci_reports if not already present."""
    print("[beaci discover] checking known PDFs …", flush=True)
    inserted = 0
    for url, reg, _year_hint in KNOWN_PDFS:
        cid = case_id_for(reg)
        if conn.execute("SELECT 1 FROM beaci_reports WHERE case_id=?", (cid,)).fetchone():
            print(f"  already known: {cid}", flush=True)
            continue
        ts = now_ms()
        conn.execute(
            "INSERT INTO beaci_reports (case_id, source_url, registration, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, url, reg, LANG, "new", ts, ts)
        )
        inserted += 1
        print(f"  + {cid}  {url}", flush=True)
    conn.commit()
    print(f"[beaci discover] inserted {inserted} new rows", flush=True)
    return inserted


def fetch(conn, client):
    """Download PDFs for all status='new' rows."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, source_url FROM beaci_reports WHERE status='new'"
    ).fetchall()
    print(f"[beaci fetch] {len(rows)} rows to fetch …", flush=True)
    ok = 0
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        dest = os.path.join(PDFDIR, cid + ".pdf")
        try:
            time.sleep(DELAY)
            print(f"  fetching {cid} …", flush=True)
            r = client.get(url, timeout=60)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
            conn.execute(
                "UPDATE beaci_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now_ms(), cid)
            )
            conn.commit()
            print(f"  fetched {cid} ({len(r.content)//1024} KB)", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [error] {cid}: {e}", file=sys.stderr)
    print(f"[beaci fetch] ok={ok}/{len(rows)}", flush=True)
    return ok


def parse(conn):
    """Extract text from downloaded PDFs and backfill metadata."""
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, event_date, source_url "
        "FROM beaci_reports WHERE status='fetched'"
    ).fetchall()
    print(f"[beaci parse] {len(rows)} rows to parse …", flush=True)
    processed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"  parsing {cid} …", flush=True)

        text = pdftotext(pdf_path) if pdf_path else ""
        scanned = False

        if len(text) < 200 and pdf_path:
            print(f"  [beaci parse] {cid}: short text ({len(text)} chars), trying OCR …", flush=True)
            ocr_text = ocr_remote(pdf_path, lang="fra")
            if ocr_text:
                text = ocr_text
                scanned = True
                print(f"  [ocr] {cid}: got {len(text)} chars after OCR", flush=True)

        # Extract / backfill metadata
        registration = row["registration"]
        if not registration:
            registration = extract_registration(text)

        event_date = row["event_date"]
        if not event_date:
            event_date = extract_date_fr(text)
        if not event_date:
            event_date = extract_date_hint_from_filename(row["source_url"] or "")

        # Extract aircraft type from text (first Airbus/Boeing/etc mention after occurrence header)
        aircraft = None
        m = re.search(r'\b(Airbus|Boeing|ATR|Cessna|Piper|Beechcraft|Bell|de Havilland|Fokker|Embraer|Bombardier|McDonnell|Antonov|Tupolev|Ilyushin|CASA|Dash)\s*[\w\- ]{1,30}', text, re.I)
        if m:
            aircraft = m.group(0)[:80].strip()

        # Extract location: "à/au/sur [lieu]" near date mention
        location = None
        m = re.search(r'(?:localis[eé]|accident[ée]?|crash|crash)\s+(?:\w+\s+){0,3}(?:à|au|sur|près de|near)\s+([A-ZÀ-Ü][^\n.,;]{3,50})', text, re.I)
        if not m:
            m = re.search(r'(?:à|au|sur|en)\s+([A-ZÀ-Ü][A-Za-zÀ-ü\s\']{3,40})(?:\s*[,\n])', text)
        if m:
            location = m.group(1).strip()[:100]

        # Extract operator
        operator = None
        for pat in [
            r'(?:compagnie|operateur|operator|exploitant)[:\s]+([A-ZÀ-Ü][^\n,;]{3,60})',
            r'(?:Air Ivoire|Air Côte d\'Ivoire|Kenya Airways|MEA|Ethiopian|Air France)[^\n]{0,40}',
        ]:
            m = re.search(pat, text, re.I)
            if m:
                operator = m.group(0)[:80].strip()
                break

        skip_reason = None
        if len(text) < FLOOR:
            skip_reason = f"short_text:{len(text)}"

        conn.execute(
            "UPDATE beaci_reports SET narrative_text=?, registration=?, event_date=?, "
            "aircraft=?, location=?, operator=?, status='parsed', skip_reason=?, updated_at=? "
            "WHERE case_id=?",
            (text, registration, event_date, aircraft, location, operator,
             skip_reason, now_ms(), cid)
        )
        conn.commit()
        print(f"  parsed {cid}: {len(text)} chars, date={event_date}, scanned={scanned}", flush=True)
        processed += 1
    print(f"[beaci parse] processed={processed}", flush=True)
    return processed


def build(conn):
    """Emit beaci_accidents rows from parsed reports."""
    rows = conn.execute(
        "SELECT case_id, source_url, registration, event_date, aircraft, "
        "operator, location, narrative_text, probable_cause, skip_reason "
        "FROM beaci_reports WHERE status='parsed'"
    ).fetchall()
    print(f"[beaci build] {len(rows)} rows to build …", flush=True)
    built = 0
    skipped = 0
    for row in rows:
        cid = row["case_id"]
        narr = row["narrative_text"] or ""
        if len(narr) < FLOOR:
            print(f"  skip {cid}: narrative {len(narr)} < {FLOOR} (reason: {row['skip_reason']})", flush=True)
            conn.execute("UPDATE beaci_reports SET status='skipped', updated_at=? WHERE case_id=?",
                         (now_ms(), cid))
            conn.commit()
            skipped += 1
            continue

        # Derive site_slug from registration (lowercase, hyphen-sep)
        reg = row["registration"] or cid.replace(SOURCE + "-", "")
        site_slug = reg.lower().replace(" ", "-")

        conn.execute(
            "INSERT OR REPLACE INTO beaci_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, site_slug, lang, "
            "fatalities_total, phase, category, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                row["event_date"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                COUNTRY,
                narr,
                row["probable_cause"],
                row["source_url"],
                "Rapport Final",
                site_slug,
                LANG,
                None,  # fatalities_total
                None,  # phase
                None,  # category
                now_ms(),
            )
        )
        conn.execute("UPDATE beaci_reports SET status='built', updated_at=? WHERE case_id=?",
                     (now_ms(), cid))
        conn.commit()
        print(f"  built {cid}: date={row['event_date']}, {len(narr)} chars", flush=True)
        built += 1

    print(f"[beaci build] built={built} skipped={skipped}", flush=True)
    return built


def verify(conn):
    """Print verification summary."""
    print("\n=== beaci VERIFY ===", flush=True)
    total = conn.execute("SELECT COUNT(*) FROM beaci_accidents").fetchone()[0]
    dated = conn.execute("SELECT COUNT(*) FROM beaci_accidents WHERE event_date IS NOT NULL").fetchone()[0]
    floor_ok = conn.execute(
        f"SELECT COUNT(*) FROM beaci_accidents WHERE length(narrative_text) >= {FLOOR}"
    ).fetchone()[0]
    dups = conn.execute(
        "SELECT case_id, COUNT(*) c FROM beaci_accidents GROUP BY case_id HAVING c>1"
    ).fetchall()
    print(f"  total rows:      {total}", flush=True)
    print(f"  ≥{FLOOR} chars:       {floor_ok}/{total}", flush=True)
    print(f"  dated:           {dated}/{total}", flush=True)
    print(f"  duplicates:      {len(dups)}", flush=True)
    if dups:
        for d in dups:
            print(f"    DUP: {d['case_id']}", flush=True)
    samples = conn.execute(
        "SELECT case_id, event_date, length(narrative_text) len FROM beaci_accidents ORDER BY event_date LIMIT 10"
    ).fetchall()
    print("  samples:", flush=True)
    for s in samples:
        print(f"    {s['case_id']}  date={s['event_date']}  len={s['len']}", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="beaci ingest")
    parser.add_argument("stage", nargs="?", default="all",
                        choices=["discover", "fetch", "parse", "build", "verify", "all"])
    args = parser.parse_args()

    conn = get_conn()
    client = get_client()

    stage = args.stage
    print(f"[beaci] stage={stage}", flush=True)

    if stage in ("discover", "all"):
        discover(conn)

    if stage in ("fetch", "all"):
        fetch(conn, client)

    if stage in ("parse", "all"):
        parse(conn)

    if stage in ("build", "all"):
        build(conn)

    if stage in ("verify", "all"):
        verify(conn)

    print("[beaci] done.", flush=True)


if __name__ == "__main__":
    main()
