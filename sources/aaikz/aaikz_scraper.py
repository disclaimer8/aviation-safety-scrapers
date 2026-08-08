#!/usr/bin/env python3
"""aaikz (Kazakhstan – Министерство транспорта РК / ДРПИТ, country KZ, lang 'ru')
aviation-accident ingest.

Source: www.gov.kz — official Kazakhstan government portal.
Reports are uploaded as hash-named PDFs under /uploads/YYYY/M/D/hash_original.SIZE.pdf

Known investigation reports (post-2019):
  1. Bek Air Z9-2100, Fokker F28 Mk0100 UP-F1007, 27.12.2019, Almaty
     Окончательный отчет, ~April 2022
     https://www.gov.kz/uploads/2022/4/29/62c1f275b6c99d882b56b7322d737fde_original.1909287.pdf

  2. Azerbaijan Airlines J2-8243, Embraer ERJ 190-100 IGW 4K-AZ65, 25.12.2024, near Aktau
     Предварительный отчет, February 2025
     https://www.gov.kz/uploads/2025/2/4/84f9ee83af415a658fc3d2830d317889_original.3875924.pdf

case_id = 'aaikz-' + flight_number.lower() + '-' + YYYYMMDD
         e.g. 'aaikz-z9-2100-20191227'

No discovery loop needed – fixed manifest of known reports.
Narrative text extracted from PDF via pdftotext; OCR via OCR_REMOTE if needed.
FLOOR = 300 chars minimum.
"""

import sys, os, re, time, sqlite3, subprocess, json
from datetime import datetime

SRC = "aaikz"
COUNTRY = "KZ"
LANG = "ru"
FLOOR = 300

HOME = os.path.expanduser(f"~/aaikz-ingest")
DB   = os.path.join(HOME, "aaikz.db")
PDFDIR = os.path.join(HOME, "pdfs")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# ---- MANIFEST ---------------------------------------------------------------
# Each entry: (case_id, event_date, flight_number, aircraft, registration,
#              operator, location, report_type, source_url)
MANIFEST = [
    {
        "case_id":     "aaikz-z9-2100-20191227",
        "event_date":  "2019-12-27",
        "flight_nr":   "Z9-2100",
        "aircraft":    "Fokker F28 Mk0100",
        "registration":"UP-F1007",
        "operator":    "АО «Бек Эйр»",
        "location":    "Аэропорт Алматы, Казахстан",
        "report_type": "Окончательный отчет",
        "source_url":  "https://www.gov.kz/uploads/2022/4/29/"
                       "62c1f275b6c99d882b56b7322d737fde_original.1909287.pdf",
    },
    {
        "case_id":     "aaikz-j2-8243-20241225",
        "event_date":  "2024-12-25",
        "flight_nr":   "J2-8243",
        "aircraft":    "Embraer ERJ 190-100 IGW",
        "registration":"4K-AZ65",
        "operator":    "ЗАО «Азербайджан Хава Йоллары» (Azerbaijan Airlines)",
        "location":    "Мангистауская область, близ Актау, Казахстан",
        "report_type": "Предварительный отчет",
        "source_url":  "https://www.gov.kz/uploads/2025/2/4/"
                       "84f9ee83af415a658fc3d2830d317889_original.3875924.pdf",
    },
]

# ---- SCHEMA -----------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS aaikz_reports (
  case_id        TEXT PRIMARY KEY,
  flight_nr      TEXT,
  registration   TEXT,
  source_url     TEXT,
  pdf_path       TEXT,
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  report_type    TEXT,
  lang           TEXT DEFAULT 'ru',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS aaikz_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'KZ',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'ru',
  built_at       INT
);
"""

# ---- HELPERS ----------------------------------------------------------------

def now():
    return int(time.time())

def conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def download_pdf(url, dest):
    """Fetch PDF with curl --max-time 15 --connect-timeout 8."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    r = subprocess.run(
        ["curl", "-sL", "--max-time", "15", "--connect-timeout", "8",
         "-A", UA, "-o", dest, "-w", "%{http_code}", url],
        capture_output=True, text=True,
    )
    code = r.stdout.strip()
    ok = code == "200" and os.path.exists(dest) and os.path.getsize(dest) > 1000
    print(f"  curl {url[-60:]} → HTTP {code}, "
          f"size={os.path.getsize(dest) if os.path.exists(dest) else 0}", flush=True)
    return ok

def extract_text(pdf_path):
    """Extract text via pdftotext; fall back to OCR_REMOTE if empty."""
    r = subprocess.run(["pdftotext", "-layout", pdf_path, "-"],
                       capture_output=True, text=True)
    txt = r.stdout.strip()
    if len(txt) >= FLOOR:
        return txt, "pdftotext"

    # Try OCR via ocrmypdf (remote if OCR_REMOTE set)
    ocr_remote = os.environ.get("OCR_REMOTE", "")
    ocr_out = pdf_path.replace(".pdf", "_ocr.pdf")
    cmd = ["ocrmypdf", "--skip-text", "--rotate-pages", "--deskew",
           "--quiet", pdf_path, ocr_out]
    env = dict(os.environ)
    if ocr_remote:
        env["OCR_REMOTE"] = ocr_remote
    sr = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if sr.returncode == 0 and os.path.exists(ocr_out):
        r2 = subprocess.run(["pdftotext", "-layout", ocr_out, "-"],
                            capture_output=True, text=True)
        txt2 = r2.stdout.strip()
        if len(txt2) >= FLOOR:
            return txt2, "ocr"
    return txt, "pdftotext"  # return what we have even if short

def extract_probable_cause(text):
    """Extract probable cause from Russian investigation report."""
    # Look for Причины / Вероятная причина / section 3.2
    patterns = [
        r'3\.2\.\s*Причин[ыа]\s*\n(.*?)(?:\n4\.|$)',
        r'Вероятн[ыа][яе]\s*причин[аы][^\n]*\n(.*?)(?:\n\d+\.|$)',
        r'Причиной[^\n]+\n(.*?)(?:\n(?:Способствующ|4\.|5\.))',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            pc = m.group(0)
            pc = re.sub(r'\s+', ' ', pc).strip()
            if len(pc) >= 50:
                return pc[:4000]
    # Fallback: look for keyword block
    idx = text.find('3.2. Причин')
    if idx >= 0:
        chunk = text[idx:idx+2000]
        chunk = re.sub(r'\s+', ' ', chunk).strip()
        return chunk
    return None

# ---- DISCOVER ---------------------------------------------------------------

def discover(c):
    """Seed the reports table from the manifest."""
    print(f"[aaikz discover] seeding {len(MANIFEST)} known reports", flush=True)
    for entry in MANIFEST:
        existing = c.execute(
            "SELECT case_id FROM aaikz_reports WHERE case_id=?",
            (entry["case_id"],)
        ).fetchone()
        if existing:
            print(f"  already known: {entry['case_id']}", flush=True)
            continue
        c.execute(
            """INSERT INTO aaikz_reports
               (case_id, flight_nr, registration, source_url, event_date,
                aircraft, operator, location, report_type, lang, status, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (
                entry["case_id"], entry["flight_nr"], entry["registration"],
                entry["source_url"], entry["event_date"], entry["aircraft"],
                entry["operator"], entry["location"], entry["report_type"],
                LANG, now(), now(),
            ),
        )
        c.commit()
        print(f"  seeded: {entry['case_id']}", flush=True)

# ---- FETCH ------------------------------------------------------------------

def fetch(c):
    """Download PDFs for all 'new' report rows."""
    rows = c.execute(
        "SELECT case_id, source_url FROM aaikz_reports WHERE status='new'"
    ).fetchall()
    print(f"[aaikz fetch] {len(rows)} to download", flush=True)
    for row in rows:
        cid = row["case_id"]
        url = row["source_url"]
        fname = os.path.join(PDFDIR, f"{cid}.pdf")
        ok = download_pdf(url, fname)
        if ok:
            c.execute(
                "UPDATE aaikz_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (fname, now(), cid),
            )
        else:
            # Try Wayback fallback
            print(f"  direct fetch failed for {cid}, trying Wayback CDX...", flush=True)
            cdx_url = (
                f"https://web.archive.org/cdx/search/cdx"
                f"?url={url}&output=json&limit=1&fl=timestamp&filter=statuscode:200"
            )
            r = subprocess.run(
                ["curl", "-sL", "--max-time", "15", "--connect-timeout", "8", cdx_url],
                capture_output=True, text=True,
            )
            try:
                data = json.loads(r.stdout)
                if len(data) > 1:
                    ts = data[1][0]
                    wb_url = f"https://web.archive.org/web/{ts}id_/{url}"
                    ok2 = download_pdf(wb_url, fname)
                    if ok2:
                        c.execute(
                            "UPDATE aaikz_reports SET pdf_path=?, source_url=?, "
                            "status='fetched', updated_at=? WHERE case_id=?",
                            (fname, wb_url, now(), cid),
                        )
                        c.commit()
                        continue
            except Exception:
                pass
            c.execute(
                "UPDATE aaikz_reports SET status='skipped', skip_reason='fetch-failed', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
        c.commit()

# ---- PARSE ------------------------------------------------------------------

def parse(c):
    """Extract narrative and metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, registration FROM aaikz_reports "
        "WHERE status='fetched'"
    ).fetchall()
    print(f"[aaikz parse] {len(rows)} to parse", flush=True)
    for row in rows:
        cid = row["case_id"]
        pdf = row["pdf_path"]
        print(f"  parsing {cid} ...", flush=True)

        txt, method = extract_text(pdf)
        print(f"    text_len={len(txt)} method={method}", flush=True)

        if len(txt) < FLOOR:
            c.execute(
                "UPDATE aaikz_reports SET status='skipped', skip_reason='no-text', "
                "narrative_text=?, updated_at=? WHERE case_id=?",
                (txt or None, now(), cid),
            )
            c.commit()
            continue

        probable_cause = extract_probable_cause(txt)

        c.execute(
            """UPDATE aaikz_reports
               SET narrative_text=?, probable_cause=?, status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, now(), cid),
        )
        c.commit()
        print(f"    parsed OK, cause={'yes' if probable_cause else 'no'}", flush=True)

# ---- BUILD ------------------------------------------------------------------

def build(c):
    """Write aaikz_accidents from parsed rows."""
    rows = c.execute(
        """SELECT r.case_id, r.event_date, r.aircraft, r.registration, r.operator,
                  r.location, r.narrative_text, r.probable_cause,
                  r.source_url, r.report_type, r.lang
           FROM aaikz_reports r WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE aaikz_reports SET status='skipped', skip_reason='floor', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        slug = r["case_id"].lower()
        c.execute(
            """INSERT OR REPLACE INTO aaikz_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url,
                report_type, site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'KZ', ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["case_id"],
                r["event_date"],
                r["aircraft"],
                r["registration"],
                r["operator"],
                r["location"],
                narr,
                r["probable_cause"],
                r["source_url"],
                r["report_type"],
                slug,
                r["lang"] or LANG,
                now(),
            ),
        )
        c.execute(
            "UPDATE aaikz_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[aaikz build] built={built}", flush=True)
    return built

# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- aaikz_reports status ---", flush=True)
    for row in c.execute(
        "SELECT status, COUNT(*) n FROM aaikz_reports GROUP BY status"
    ):
        print(f"  {row['status']:12s} {row['n']}", flush=True)

    rows = c.execute("SELECT COUNT(*) n FROM aaikz_accidents").fetchone()
    if rows and rows["n"] > 0:
        print(f"\n--- aaikz_accidents: {rows['n']} rows ---", flush=True)
        null_dates = c.execute(
            "SELECT COUNT(*) n FROM aaikz_accidents WHERE event_date IS NULL"
        ).fetchone()["n"]
        dups = c.execute(
            "SELECT COUNT(*) n FROM (SELECT case_id, COUNT(*) c FROM aaikz_accidents "
            "GROUP BY case_id HAVING c > 1)"
        ).fetchone()["n"]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM aaikz_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  dups: {dups}  "
              f"narr_len min={narr[0]} max={narr[1]}", flush=True)
        print("\n  sample rows:", flush=True)
        for r in c.execute(
            "SELECT case_id, registration, event_date, report_type, "
            "LENGTH(narrative_text) len FROM aaikz_accidents ORDER BY event_date"
        ):
            print(f"    {r['case_id']:35s}  reg={r['registration'] or 'NULL':12s}  "
                  f"date={r['event_date'] or 'NULL'}  type={r['report_type']}  "
                  f"narr={r['len']}", flush=True)

    skipped = c.execute(
        "SELECT case_id, skip_reason FROM aaikz_reports WHERE status='skipped'"
    ).fetchall()
    if skipped:
        print(f"\n  skipped ({len(skipped)}): "
              f"{[(r['case_id'], r['skip_reason']) for r in skipped]}", flush=True)

# ---- MAIN -------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "all"):
        discover(c)

    if mode in ("fetch", "all"):
        fetch(c)

    if mode in ("parse", "all"):
        parse(c)

    if mode in ("build", "all"):
        build(c)

    print_stats(c)


if __name__ == "__main__":
    main()
