#!/usr/bin/env python3
"""beabj — Bureau Enquêtes-Accidents du Bénin (BEA-Bénin)
aviation-accident ingest.  country BJ, lang 'fr'.

Listing: https://bea.bj/enquetes-rapports/
WP media API scan cross-checked (no additional accident reports found).

Known PDFs (5 total, 5 distinct events):
  Accident PDFs (final reports):
    Rapport-denquete-du-crash-dhelicoptere-AW-139-immatricule-TY-ABC-…2015.pdf
       TY-ABC  AW139  2015-12-26  (published 2025, helicopter crash)
    Rapport-final-denquete-incident-grave-…ULM…VG-BEN-01-APN…2023.pdf
       VG-BEN-01-APN  ULM Savannah  2023-10-25  (serious incident)
    rapport_accident_avion_benin_25_dec_2023.pdf
       3X-GDO  Boeing 727-223  2003-12-25  (filename has upload date 2023, event is 2003)

  Sikorsky N703HG open investigation (preliminary + interim — NO final yet):
    COMPTE-RENDU-PRELIMINAIRE-DUN-ACCIDENT-DU-N703HG-SIKORSKY-S-61N.pdf
       N703HG  Sikorsky S-61N  preliminary — ingest as report_type='Compte rendu préliminaire'
    DECLARATION-INTERMEDIAIRE-PUBLIQUE-SUR-ACCIDENT-DU-N703HG-SIKORSKY-S-61N_ANNEE-1.pdf
       N703HG  intermediate public statement — superseded_by when final arrives

Supersession: COMPTE-RENDU-PRELIMINAIRE and DECLARATION-INTERMEDIAIRE for N703HG
  are the same accident.  Keep both rows (different doc types), mark preliminary
  as such.  When final report is published: add it and set superseded_by on these.

case_id = 'beabj-' + registration.lower().replace(' ', '-').replace('/', '-')
  For N703HG two docs → 'beabj-n703hg-prelim' and 'beabj-n703hg-interim'
  to avoid PK collision.

event_date: extracted from French date patterns in PDF text.
  Fallback: date hint from filename slug.

OCR: if pdftotext yields < FLOOR chars, run remote OCR on hetzner with lang fra.
     OCR_REMOTE env must be set to '<ocr-host>'.

Stages: discover | fetch | parse | build | verify | all
"""

import sys, os, re, time, sqlite3, subprocess, uuid

SOURCE  = "beabj"
COUNTRY = "BJ"
LANG    = "fr"
HOME    = os.path.expanduser(f"~/beabj-ingest")
DB      = os.path.join(HOME, "beabj.db")
PDFDIR  = os.path.join(HOME, "pdfs")
FLOOR   = 300   # narrative floor
UA      = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DELAY   = 2.0

# All known PDFs.  Each tuple: (url, case_id_suffix, registration_hint, report_type)
# case_id will be  SOURCE + '-' + case_id_suffix
KNOWN_PDFS = [
    (
        "https://bea.bj/wp-content/uploads/2025/07/"
        "Rapport-denquete-du-crash-dhelicoptere-AW-139-immatricule-TY-ABC-26-decembre-2015-"
        "au-Stade-Atchoucouma-de-Djougou.pdf",
        "ty-abc",         # case_id suffix
        "TY-ABC",         # registration hint
        "Rapport d'enquête",
        False,            # is_preliminary
    ),
    (
        "https://bea.bj/wp-content/uploads/2025/07/"
        "Rapport-final-denquete-incident-grave-Survenu-a-ULM-SAVANNAH-VG-BEN-01-APN-le-25-"
        "octobre-2023-a-Barabon-dans-le-Parc-W.pdf",
        "vg-ben-01-apn",
        "VG-BEN-01-APN",
        "Rapport final d'enquête",
        False,
    ),
    (
        "https://bea.bj/wp-content/uploads/2024/07/"
        "rapport_accident_avion_benin_25_dec_2023.pdf",
        "3x-gdo",         # reg=3X-GDO Boeing 727-223; filename date is upload year not event
        "3X-GDO",
        "Rapport d'accident",
        False,
    ),
    (
        "https://bea.bj/wp-content/uploads/2026/01/"
        "COMPTE-RENDU-PRELIMINAIRE-DUN-ACCIDENT-DU-N703HG-SIKORSKY-S-61N.pdf",
        "n703hg-prelim",
        "N703HG",
        "Compte rendu préliminaire",
        True,
    ),
    (
        "https://bea.bj/wp-content/uploads/2026/01/"
        "DECLARATION-INTERMEDIAIRE-PUBLIQUE-SUR-ACCIDENT-DU-N703HG-SIKORSKY-S-61N_ANNEE-1.pdf",
        "n703hg-interim",
        "N703HG",
        "Déclaration intermédiaire publique",
        True,
    ),
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
_DATE_YMD = re.compile(r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b")
_DATE_DMY = re.compile(r"\b([0-3]?\d)[./\-]([01]\d)[./\-]((?:19|20)\d{2})\b")

# Registration patterns: Beninese TY-XXX, foreign N703HG, VG-BEN-01-APN (ultralight French)
_REG_PATTERNS = re.compile(
    r"\b(TY-[A-Z]{3}|N\d{3,5}[A-Z]{0,2}|[A-Z]{1,2}-[A-Z]{3,4}|VG-BEN-\d{2}-[A-Z]{3}|F-[A-Z]{4}|[A-Z]{1,2}\d{3,5})\b"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS beabj_reports (
  case_id        TEXT PRIMARY KEY,
  source_url     TEXT,
  pdf_path       TEXT,
  registration   TEXT,
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  report_type    TEXT,
  is_preliminary INT DEFAULT 0,
  superseded_by  TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang           TEXT DEFAULT 'fr',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS beabj_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'BJ',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  fatalities_total INT,
  phase          TEXT,
  category       TEXT,
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_beabj_status ON beabj_reports(status);
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
    """Extract partial date from URL/filename."""
    fn = url.split("/")[-1].lower()
    # Pattern: 25_dec_2023 or 26-decembre-2015
    m = re.search(r'(\d{1,2})[_\-](jan|fev|mars|avr|mai|juin|juil|aou|dec|sep|oct|nov|d[ée]cembre|janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre)(?:[_\-])(\d{4})', fn)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)[:3]
        yr = int(m.group(3))
        month_map = {"jan":1,"fev":2,"mar":3,"avr":4,"mai":5,"jui":6,
                     "jui":7,"aou":8,"sep":9,"oct":10,"nov":11,"dec":12,"dé":12,"dè":12}
        mo = month_map.get(mon_str, 0)
        if mo and 1950 <= yr <= 2030:
            return f"{yr}-{mo:02d}-{day:02d}"
    # simple YYYY_MM_DD or YYYY-MM-DD in filename
    m = re.search(r'(\d{4})[_\-](\d{2})[_\-](\d{2})', fn)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1950 <= y <= 2030:
            return f"{y}-{mo:02d}-{d:02d}"
    return None


def extract_registration(text):
    for m in _REG_PATTERNS.finditer(text):
        reg = m.group(1)
        return reg
    return None


def pdftotext(pdf_path):
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
    host = os.environ.get("OCR_REMOTE", "")
    if not host:
        print(f"  [ocr] OCR_REMOTE not set, skipping for {pdf_path}", file=sys.stderr)
        return ""
    remote_tmp = f"/tmp/ocr-{uuid.uuid4().hex}.pdf"
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
    """Insert known PDFs into beabj_reports."""
    print("[beabj discover] checking known PDFs …", flush=True)
    inserted = 0
    for url, suffix, reg_hint, report_type, is_prelim in KNOWN_PDFS:
        cid = SOURCE + "-" + suffix
        if conn.execute("SELECT 1 FROM beabj_reports WHERE case_id=?", (cid,)).fetchone():
            print(f"  already known: {cid}", flush=True)
            continue
        ts = now_ms()
        conn.execute(
            "INSERT INTO beabj_reports "
            "(case_id, source_url, registration, report_type, is_preliminary, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, url, reg_hint, report_type, 1 if is_prelim else 0, LANG, "new", ts, ts)
        )
        inserted += 1
        print(f"  + {cid}  {url}", flush=True)
    conn.commit()

    # Mark N703HG-interim as superseded_by prelim (same accident, newer status)
    # When final arrives, update both.
    conn.execute(
        "UPDATE beabj_reports SET superseded_by=? WHERE case_id=? AND superseded_by IS NULL",
        ("beabj-n703hg-prelim", "beabj-n703hg-interim")
    )
    conn.commit()

    print(f"[beabj discover] inserted {inserted} new rows", flush=True)
    return inserted


def fetch(conn, client):
    """Download PDFs for status='new' rows."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, source_url FROM beabj_reports WHERE status='new'"
    ).fetchall()
    print(f"[beabj fetch] {len(rows)} rows to fetch …", flush=True)
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
                "UPDATE beabj_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now_ms(), cid)
            )
            conn.commit()
            print(f"  fetched {cid} ({len(r.content)//1024} KB)", flush=True)
            ok += 1
        except Exception as e:
            print(f"  [error] {cid}: {e}", file=sys.stderr)
    print(f"[beabj fetch] ok={ok}/{len(rows)}", flush=True)
    return ok


def parse(conn):
    """Extract text from PDFs and backfill metadata."""
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, event_date, source_url, is_preliminary "
        "FROM beabj_reports WHERE status='fetched'"
    ).fetchall()
    print(f"[beabj parse] {len(rows)} rows to parse …", flush=True)
    processed = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"  parsing {cid} …", flush=True)

        text = pdftotext(pdf_path) if pdf_path else ""
        scanned = False

        if len(text) < 200 and pdf_path:
            print(f"  [beabj parse] {cid}: short text ({len(text)} chars), trying OCR …", flush=True)
            ocr_text = ocr_remote(pdf_path, lang="fra")
            if ocr_text:
                text = ocr_text
                scanned = True
                print(f"  [ocr] {cid}: got {len(text)} chars after OCR", flush=True)

        registration = row["registration"]
        if not registration:
            registration = extract_registration(text)

        event_date = row["event_date"]
        if not event_date:
            event_date = extract_date_fr(text)
        if not event_date:
            event_date = extract_date_hint_from_filename(row["source_url"] or "")
        # For the N703HG interim statement the first date in text is the publication date
        # (10 Janvier 2026); the actual event date is 2025-01-10.
        if cid == "beabj-n703hg-interim":
            event_date = "2025-01-10"

        aircraft = None
        m = re.search(r'\b(AgustaWestland|Leonardo|AW\s*139|Sikorsky\s*S-61|ULM|Savannah|Airbus|Boeing|ATR|Cessna|Piper|Bell|de Havilland|Fokker|Embraer|Bombardier|Antonov|Robinson|EC\s*135|AW\s*\d{3})\s*[\w\-\. ]{0,30}', text, re.I)
        if m:
            aircraft = m.group(0)[:80].strip()
        # Normalise known types from text fragments
        _AIRCRAFT_NORM = {
            "beabj-ty-abc": "AgustaWestland AW139",
            "beabj-vg-ben-01-apn": "ULM Savannah VG",
            "beabj-3x-gdo": "Boeing 727-223",
            "beabj-n703hg-prelim": "Sikorsky S-61N",
            "beabj-n703hg-interim": "Sikorsky S-61N",
        }
        if cid in _AIRCRAFT_NORM:
            aircraft = _AIRCRAFT_NORM[cid]

        location = None
        for pat in [
            r'(?:à|au|sur|près de)\s+([A-ZÀ-Ü][A-Za-zÀ-ü\s\']{3,50})(?:[,\n])',
            r'(?:Djougou|Cotonou|Porto-Novo|Parakou|Natitingou|Bohicon|Kandi|Abomey)',
        ]:
            m2 = re.search(pat, text, re.I)
            if m2:
                location = m2.group(0)[:80].strip()
                break

        operator = None
        for pat in [
            r'(?:exploitant|operator|compagnie|transporteur)[:\s]+([A-ZÀ-Ü][^\n,;]{3,60})',
        ]:
            m2 = re.search(pat, text, re.I)
            if m2:
                operator = m2.group(1)[:80].strip()
                break

        skip_reason = None
        if len(text) < FLOOR and not row["is_preliminary"]:
            skip_reason = f"short_text:{len(text)}"
        elif len(text) < FLOOR and row["is_preliminary"]:
            # Preliminary reports may be shorter — still ingest
            skip_reason = None

        conn.execute(
            "UPDATE beabj_reports SET narrative_text=?, registration=?, event_date=?, "
            "aircraft=?, location=?, operator=?, status='parsed', skip_reason=?, updated_at=? "
            "WHERE case_id=?",
            (text, registration, event_date, aircraft, location, operator,
             skip_reason, now_ms(), cid)
        )
        conn.commit()
        print(f"  parsed {cid}: {len(text)} chars, date={event_date}, scanned={scanned}", flush=True)
        processed += 1
    print(f"[beabj parse] processed={processed}", flush=True)
    return processed


def build(conn):
    """Emit beabj_accidents rows from parsed reports."""
    rows = conn.execute(
        "SELECT case_id, source_url, registration, event_date, aircraft, "
        "operator, location, narrative_text, probable_cause, skip_reason, "
        "report_type, is_preliminary "
        "FROM beabj_reports WHERE status='parsed'"
    ).fetchall()
    print(f"[beabj build] {len(rows)} rows to build …", flush=True)
    built = 0
    skipped = 0
    for row in rows:
        cid = row["case_id"]
        narr = row["narrative_text"] or ""
        # Preliminary reports: lower floor (100 chars) since they're intentionally brief
        eff_floor = 100 if row["is_preliminary"] else FLOOR
        if len(narr) < eff_floor:
            print(f"  skip {cid}: narrative {len(narr)} < {eff_floor}", flush=True)
            conn.execute("UPDATE beabj_reports SET status='skipped', updated_at=? WHERE case_id=?",
                         (now_ms(), cid))
            conn.commit()
            skipped += 1
            continue

        reg = row["registration"] or cid.replace(SOURCE + "-", "")
        site_slug = reg.lower().replace(" ", "-").replace("/", "-") if reg else cid.replace(SOURCE + "-", "")

        conn.execute(
            "INSERT OR REPLACE INTO beabj_accidents "
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
                row["report_type"],
                site_slug,
                LANG,
                None,
                None,
                None,
                now_ms(),
            )
        )
        conn.execute("UPDATE beabj_reports SET status='built', updated_at=? WHERE case_id=?",
                     (now_ms(), cid))
        conn.commit()
        print(f"  built {cid}: date={row['event_date']}, {len(narr)} chars", flush=True)
        built += 1

    print(f"[beabj build] built={built} skipped={skipped}", flush=True)
    return built


def verify(conn):
    """Print verification summary."""
    print("\n=== beabj VERIFY ===", flush=True)
    total = conn.execute("SELECT COUNT(*) FROM beabj_accidents").fetchone()[0]
    dated = conn.execute("SELECT COUNT(*) FROM beabj_accidents WHERE event_date IS NOT NULL").fetchone()[0]
    floor_ok = conn.execute(
        f"SELECT COUNT(*) FROM beabj_accidents WHERE length(narrative_text) >= {FLOOR}"
    ).fetchone()[0]
    dups = conn.execute(
        "SELECT case_id, COUNT(*) c FROM beabj_accidents GROUP BY case_id HAVING c>1"
    ).fetchall()
    print(f"  total rows:      {total}", flush=True)
    print(f"  ≥{FLOOR} chars:       {floor_ok}/{total}", flush=True)
    print(f"  dated:           {dated}/{total}", flush=True)
    print(f"  duplicates:      {len(dups)}", flush=True)
    if dups:
        for d in dups:
            print(f"    DUP: {d['case_id']}", flush=True)
    samples = conn.execute(
        "SELECT case_id, event_date, report_type, length(narrative_text) len "
        "FROM beabj_accidents ORDER BY event_date"
    ).fetchall()
    print("  samples:", flush=True)
    for s in samples:
        print(f"    {s['case_id']}  date={s['event_date']}  type={s['report_type']}  len={s['len']}", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="beabj ingest")
    parser.add_argument("stage", nargs="?", default="all",
                        choices=["discover", "fetch", "parse", "build", "verify", "all"])
    args = parser.parse_args()

    conn = get_conn()
    client = get_client()

    stage = args.stage
    print(f"[beabj] stage={stage}", flush=True)

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

    print("[beabj] done.", flush=True)


if __name__ == "__main__":
    main()
