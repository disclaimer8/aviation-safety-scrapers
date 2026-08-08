#!/usr/bin/env python3
"""aaibly — Libya Air Accidents Investigation Bureau (5A/Libya)
aviation-accident ingest.

Primary source — Afriqiyah Airways A330 final report (LIVE via Wayback):
  URL:      https://asn.flightsafety.org/reports/2010/20100512_A332_5A-ONG.pdf
  Note:     ASN returns 403 directly from minipc; use Wayback snapshots.
  Wayback:  20241123024145 (2.4MB, HTTP 200) — largest/cleanest snapshot.
  Accident: Afriqiyah Airways flight 771, Airbus A330-202, reg. 5A-ONG
  Date:     2010-05-12
  Location: Tripoli International Airport, Libya
  Fatal:    103 (92 passengers + 11 crew); 1 survivor (9-year-old Dutch boy)

Libya AAIB site (https://aaib.mot.gov.ly/final-reports-of-air-investigations/):
  Lists 7 accident titles with NO downloadable PDFs.
  Titles only (no attached reports): TWIN OTTER DHC-6 5A-DDC (2014-01-28),
  Boeing 727 5A-DIA (1992-12-22), Airbus A319 5A-OND (2016-01-12),
  DC-10 HL7328 (1989-07-26), A330 5A-ONG (2010-05-12, this report),
  Embraer ERJ-170 5A-SOC (2013-12-06), DHC Shorts SD3-60 HB-AAM.
  Substance per listing: title+date only, well below 300 chars → text-only skip.
  Decision: only the ASN-mirrored A330 report has content; others deferred.

Old LYCAA (Libya CAA) domain lycaa.gov.ly: CDX scan found zero PDFs.

case_id = 'aaibly-5a-ong'
country = 'LY'
lang    = 'en'
FLOOR   = 300   chars

Stages: seed | fetch | parse | build.
"""
import sys, os, re, time, sqlite3, subprocess, shlex, tempfile

HOME   = os.path.expanduser("~/aaibly-ingest")
DB     = os.path.join(HOME, "aaibly.db")
PDFDIR = os.path.join(HOME, "pdfs")
FLOOR  = 300
OCR_LANG = "eng"

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"

WAYBACK_BASE = "https://web.archive.org/web"

# Known static list
KNOWN_REPORTS = [
    {
        "case_id":      "aaibly-5a-ong",
        "event_date":   "2010-05-12",
        "aircraft":     "Airbus A330-202",
        "registration": "5A-ONG",
        "operator":     "Afriqiyah Airways",
        "location":     "Tripoli International Airport, Libya",
        "source_url":   "https://asn.flightsafety.org/reports/2010/20100512_A332_5A-ONG.pdf",
        "report_type":  "Final Report",
        # ASN returns 403 directly — use Wayback snapshots
        "archive_ts":   "20241123024145",
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaibly_reports (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  source_url     TEXT,
  archive_url    TEXT,
  archive_ts     TEXT,
  pdf_path       TEXT,
  report_type    TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang           TEXT DEFAULT 'en',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS aaibly_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'LY',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'en',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_aaibly_status ON aaibly_reports(status);
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


def http():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=120.0,
        follow_redirects=True,
    )


def _ocr_remote(pdf_path, lang, host):
    remote = "/tmp/ocr-%s.pdf" % os.urandom(8).hex()
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
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(
            ["ocrmypdf", "--force-ocr", "--language", lang,
             "--sidecar", sidecar, "--output-type", "none",
             str(pdf_path), "-"],
            capture_output=True, timeout=600,
        )
        with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    finally:
        try:
            os.unlink(sidecar)
        except OSError:
            pass


def extract_text(pdf_path):
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=300,
        )
        return cp.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# ---- SEED -------------------------------------------------------------------

def seed(c):
    inserted = 0
    for r in KNOWN_REPORTS:
        existing = c.execute(
            "SELECT case_id FROM aaibly_reports WHERE case_id=?", (r["case_id"],)
        ).fetchone()
        if existing:
            print(f"  [seed] {r['case_id']} already exists", flush=True)
            continue
        c.execute(
            "INSERT INTO aaibly_reports "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "source_url, archive_ts, report_type, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'en','new',?,?)",
            (r["case_id"], r["event_date"], r["aircraft"], r["registration"],
             r["operator"], r["location"], r["source_url"],
             r["archive_ts"], r["report_type"],
             now(), now()),
        )
        c.commit()
        inserted += 1
        print(f"  [seed] inserted {r['case_id']}", flush=True)
    return inserted


# ---- FETCH ------------------------------------------------------------------

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url, archive_ts FROM aaibly_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0

    for row in rows:
        cid  = row["case_id"]
        url  = row["source_url"]
        ts   = row["archive_ts"]
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe)

        print(f"[aaibly fetch] {cid} …", flush=True)

        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            c.execute(
                "UPDATE aaibly_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
            downloaded += 1
            continue

        # ASN returns 403 directly; use Wayback
        if ts:
            fetch_url = f"{WAYBACK_BASE}/{ts}id_/{url}"
            print(f"  Wayback ts={ts}", flush=True)
        else:
            fetch_url = url
            print(f"  Direct fetch (fallback)", flush=True)

        try:
            r = cl.get(fetch_url, timeout=120)

            if r.status_code != 200 and ts:
                # Try without id_ wrapper
                fetch_url2 = f"{WAYBACK_BASE}/{ts}/{url}"
                print(f"  HTTP {r.status_code} — retry without id_ …", flush=True)
                r = cl.get(fetch_url2, timeout=120)
                if r.status_code == 200:
                    fetch_url = fetch_url2

            if r.status_code != 200:
                # Try another Wayback timestamp
                print(f"  HTTP {r.status_code} — trying 20241227 snapshot …", flush=True)
                ts2 = "20241227015745"
                fetch_url3 = f"{WAYBACK_BASE}/{ts2}id_/{url}"
                r = cl.get(fetch_url3, timeout=120)
                if r.status_code == 200:
                    fetch_url = fetch_url3

            if r.status_code != 200:
                print(f"  HTTP {r.status_code}", file=sys.stderr, flush=True)
                c.execute(
                    "UPDATE aaibly_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                    (f"http-{r.status_code}", now(), cid),
                )
                c.commit()
                continue

            content = r.content
            if content[:4] != b"%PDF":
                pdf_start = content.find(b"%PDF")
                if pdf_start > 0:
                    content = content[pdf_start:]
                else:
                    print(f"  not PDF ({content[:20]!r})", file=sys.stderr, flush=True)
                    c.execute(
                        "UPDATE aaibly_reports SET status='skipped', skip_reason='not-pdf', "
                        "updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue

            with open(dest, "wb") as fh:
                fh.write(content)

            size = os.path.getsize(dest)
            c.execute(
                "UPDATE aaibly_reports SET pdf_path=?, archive_url=?, status='fetched', updated_at=? "
                "WHERE case_id=?",
                (dest, fetch_url, now(), cid),
            )
            c.commit()
            downloaded += 1
            print(f"  saved {size//1024}KB", flush=True)

        except Exception as e:
            print(f"  exception: {e}", file=sys.stderr, flush=True)
            c.execute(
                "UPDATE aaibly_reports SET status='skipped', skip_reason=?, updated_at=? WHERE case_id=?",
                (str(e)[:120], now(), cid),
            )
            c.commit()

    print(f"[aaibly fetch] downloaded={downloaded}", flush=True)
    return downloaded


# ---- PARSE ------------------------------------------------------------------

_CAUSE_RE = re.compile(
    r"(?:probable\s+cause[s]?|cause[s]?\s+of\s+the\s+accident|"
    r"contributing\s+factor[s]?|Conclusion[s]?)[:\s]*\n?"
    r"(.*?)(?:\n{3,}|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_probable_cause(txt):
    if not txt:
        return None
    m = _CAUSE_RE.search(txt)
    if m:
        v = m.group(1).strip()
        if 20 < len(v) < 5000:
            return v[:4000]
    return None


def parse(c):
    rows = c.execute(
        "SELECT case_id, pdf_path FROM aaibly_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[aaibly parse] {cid}", flush=True)

        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  PDF missing", file=sys.stderr, flush=True)
            continue

        raw_txt = extract_text(pdf_path)
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"  pdftotext → {len(raw_txt)} chars (file={size_kb}KB)", flush=True)

        if len(raw_txt) < FLOOR:
            print(f"  text too short — trying OCR …", flush=True)
            raw_txt = ocr_extract(pdf_path)
            print(f"  OCR → {len(raw_txt)} chars", flush=True)

        if len(raw_txt) < FLOOR:
            print(f"  insufficient text → skip", file=sys.stderr, flush=True)
            c.execute(
                "UPDATE aaibly_reports SET narrative_text=?, status='skipped', "
                "skip_reason='no-text', updated_at=? WHERE case_id=?",
                (raw_txt, now(), cid),
            )
            c.commit()
            continue

        probable_cause = parse_probable_cause(raw_txt)

        c.execute(
            """UPDATE aaibly_reports SET
               narrative_text=?, probable_cause=?, status='parsed', updated_at=?
               WHERE case_id=?""",
            (raw_txt, probable_cause, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  narr={len(raw_txt)} chars  cause={'yes' if probable_cause else 'no'}", flush=True)

    print(f"[aaibly parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD ------------------------------------------------------------------

def build(c):
    rows = c.execute(
        """SELECT case_id, event_date, aircraft, registration, operator, location,
                  narrative_text, probable_cause, source_url, report_type, lang
           FROM aaibly_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE aaibly_reports SET status='skipped', skip_reason='no-text', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        slug = r["case_id"].lower()
        c.execute(
            """INSERT OR REPLACE INTO aaibly_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?,?,?,?,?,?,'LY',?,?,?,?,?,?,?)""",
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
                r["lang"] or "en",
                now(),
            ),
        )
        c.execute(
            "UPDATE aaibly_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1
        print(f"[aaibly build] {r['case_id']}  narr={len(narr)} chars", flush=True)

    print(f"[aaibly build] built={built}", flush=True)
    return built


# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- aaibly_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM aaibly_reports "
        "GROUP BY status, skip_reason"
    ):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):25s} {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM aaibly_accidents").fetchone()[0]
    print(f"\n--- aaibly_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM aaibly_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) "
            "FROM aaibly_accidents"
        ).fetchone()
        below_floor = c.execute(
            "SELECT COUNT(*) FROM aaibly_accidents WHERE LENGTH(narrative_text) < ?",
            (FLOOR,)
        ).fetchone()[0]
        dups = c.execute(
            "SELECT COUNT(*) FROM (SELECT case_id, COUNT(*) n FROM aaibly_accidents "
            "GROUP BY case_id HAVING n > 1)"
        ).fetchone()[0]
        print(f"  event_date NULL: {null_dates}  below_floor({FLOOR}): {below_floor}  dups: {dups}")
        print(f"  narr_len min={narr[0]} max={narr[1]}")
        print("\n  sample rows:")
        for row in c.execute(
            "SELECT case_id, registration, event_date, LENGTH(narrative_text) len, report_type "
            "FROM aaibly_accidents ORDER BY event_date LIMIT 10"
        ):
            print(f"    {row['case_id']:30s}  reg={row['registration'] or 'NULL':10s}  "
                  f"date={row['event_date'] or 'NULL'}  narr={row['len']}  "
                  f"type={row['report_type']}")


# ---- MAIN -------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("seed", "discover", "all"):
        seed(c)

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

    print_stats(c)


if __name__ == "__main__":
    main()
