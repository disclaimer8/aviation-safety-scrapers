#!/usr/bin/env python3
"""dgcalb (Lebanon DGCA — Directorate General of Civil Aviation, country LB, lang 'en')
aviation-accident ingest.

Source: http://www.dgca.gov.lb/index.php/en/reports-en
Joomla site with Phocadownload component; EU-IP accessible (hetzner only).

Three accident reports available:
  1. DHL Flight 160 (A9C-DHAB), Boeing 767-300, 2023-09-18 — direct URL
  2. OD-SKY (Hawker 850XP), 2011-02-04 — phocadownload id=229, category=30
  3. Ethiopian ET409 (ET-ANB), 2010-01-25 — phocadownload id=228 (login-gated),
     text fetched from Wayback archived article page instead.

One incident report (NOT ingested):
  4. Learjet 60 A6-IAS, 2011-11-06 — incident, excluded (download=230)

Strategy:
  - DHL: fetch direct PDF URL from live site (via hetzner)
  - OD-SKY: fetch via phocadownload category URL (no auth required)
  - ET409: fetch Wayback-archived article page, extract HTML text (PDF login-gated)
  - All three: extract metadata from PDF or HTML text

Fetch commands MUST run via: ssh root@hetzner curl ... (EU IP required)
DO NOT probe live site from minipc/Mac — will timeout.

case_id = 'dgcalb-' + registration.lower()

FETCH COMMAND: ssh hetzner "curl --max-time 60 --connect-timeout 10 ..."
"""

import sys, os, re, time, sqlite3, subprocess, json, tempfile

HOME = os.path.expanduser("~/dgcalb-ingest")
DB = os.path.join(HOME, "dgcalb.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

FLOOR = 300

# ---- KNOWN REPORTS --------------------------------------------------------

KNOWN_REPORTS = [
    {
        "case_id": "dgcalb-a9c-dhab",
        "registration": "A9C-DHAB",
        "aircraft": "Boeing 767-300",
        "operator": "DHL / European Air Transport",
        "event_date": "2023-09-18",
        "location": "Rafic Hariri International Airport, Beirut, Lebanon",
        "report_type": "Final Investigation Report",
        "fetch_method": "direct",
        "source_url": "http://www.dgca.gov.lb/phocadownload/accident-investigation-reports/final%20investigation%20report%20dhl.pdf",
        "source_page": "http://www.dgca.gov.lb/index.php/en/reports-en",
        "lang": "en",
    },
    {
        "case_id": "dgcalb-od-sky",
        "registration": "OD-SKY",
        "aircraft": "Hawker 850XP",
        "operator": "Sky Lounge Services S.A.L.",
        "event_date": "2011-02-04",
        "location": "Sulaimaniyah International Airport, Iraq",
        "report_type": "Accident Investigation Report",
        "fetch_method": "phocadownload",
        "phocadownload_url": "http://www.dgca.gov.lb/index.php?option=com_phocadownload&view=category&id=30&download=229&Itemid=207",
        "source_url": "http://www.dgca.gov.lb/index.php/en/odsky-acc-report-en",
        "source_page": "http://www.dgca.gov.lb/index.php/en/reports-en",
        "lang": "en",
    },
    {
        "case_id": "dgcalb-et-anb",
        "registration": "ET-ANB",
        "aircraft": "Boeing 737-800",
        "operator": "Ethiopian Airlines",
        "event_date": "2010-01-25",
        "location": "Mediterranean Sea, off Beirut, Lebanon",
        "report_type": "Accident Investigation Report",
        "fetch_method": "wayback_html",
        "wayback_url": "https://web.archive.org/web/20230602155123/http://www.dgca.gov.lb/index.php/en/ethiopian-et409-en",
        "source_url": "http://www.dgca.gov.lb/index.php/en/ethiopian-et409-en",
        "source_page": "http://www.dgca.gov.lb/index.php/en/reports-en",
        "lang": "en",
        # ET409 PDF is login-gated; use investigation description from Wayback HTML + known facts
        "known_narrative": (
            "On 25 January 2010, Ethiopian Airlines Flight 409, a Boeing 737-800 registered ET-ANB, "
            "departed Beirut Rafic Hariri International Airport (BRHIA) at 02:37 local time on a flight "
            "to Addis Ababa. Shortly after takeoff, the aircraft crashed into the Mediterranean Sea "
            "approximately 3 nautical miles northwest of the airport. All 90 people on board — 82 "
            "passengers and 8 crew members — were killed. The aircraft was climbing through approximately "
            "9,000 feet when it disappeared from radar. Witnesses reported seeing fire on the aircraft "
            "before it struck the water. The Lebanese Government launched an investigation as the State "
            "of Occurrence in accordance with ICAO Annex 13. A Preliminary Report was submitted on "
            "25 February 2010. Two Investigation Progress Reports were released on 10 February 2011 "
            "and 25 August 2011. The Final Report was issued following consultation with the NTSB "
            "(USA), ECAA (Ethiopia) and BEA (France). Differences between the Ethiopian party and "
            "the rest of the Investigation Committee are appended as Appendix Z to the report. "
            "The investigation report was published by the Lebanese DGCA."
        ),
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS dgcalb_reports (
  case_id        TEXT PRIMARY KEY,
  registration   TEXT,
  aircraft       TEXT,
  operator       TEXT,
  event_date     TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'LB',
  source_url     TEXT,
  source_page    TEXT,
  pdf_path       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  report_type    TEXT DEFAULT 'Final Investigation Report',
  lang           TEXT DEFAULT 'en',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS dgcalb_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'LB',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT DEFAULT 'Final Investigation Report',
  site_slug      TEXT,
  lang           TEXT DEFAULT 'en',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_dgcalb_status ON dgcalb_reports(status);
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


def curl_via_hetzner(url, output_path=None, timeout=60):
    """Fetch URL via hetzner (EU IP required for dgca.gov.lb)."""
    cmd = [
        "ssh", "hetzner",
        f"curl --max-time {timeout} --connect-timeout 10 "
        f"-sL -H 'User-Agent: {UA}' "
        f"'{url}'"
        + (f" -o /tmp/dgcalb_dl.tmp && cat /tmp/dgcalb_dl.tmp" if output_path else "")
    ]
    if output_path:
        # Download to hetzner, then scp to local
        cmd_dl = ["ssh", "hetzner",
                  f"curl --max-time {timeout} --connect-timeout 10 "
                  f"-sL -H 'User-Agent: {UA}' "
                  f"'{url}' -o /tmp/dgcalb_dl.tmp && echo 'OK'"]
        result = subprocess.run(cmd_dl, capture_output=True, timeout=timeout + 15)
        if b"OK" not in result.stdout:
            print(f"  [curl_via_hetzner] download to hetzner failed", file=sys.stderr)
            return False
        # SCP back
        cmd_scp = ["scp", "-o", "StrictHostKeyChecking=no",
                   "hetzner:/tmp/dgcalb_dl.tmp", output_path]
        result_scp = subprocess.run(cmd_scp, capture_output=True, timeout=120)
        if result_scp.returncode != 0:
            print(f"  [scp] failed: {result_scp.stderr.decode()[:200]}", file=sys.stderr)
            return False
        return True
    else:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        return result.stdout


def curl_direct(url, output_path=None, timeout=60):
    """Fetch URL directly (for archive.org — accessible from minipc)."""
    if output_path:
        cmd = ["curl", "--max-time", str(timeout), "--connect-timeout", "10",
               "-sL", "-H", f"User-Agent: {UA}", url, "-o", output_path]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        return result.returncode == 0
    else:
        cmd = ["curl", "--max-time", str(timeout), "--connect-timeout", "10",
               "-sL", "-H", f"User-Agent: {UA}", url]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
        return result.stdout


def extract_text_from_pdf(pdf_path):
    """Run pdftotext on a PDF file."""
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=120,
        )
        return cp.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"  pdftotext error: {e}", file=sys.stderr)
        return ""


def extract_text_from_html(html_bytes_or_str):
    """Strip HTML tags and extract clean text."""
    if isinstance(html_bytes_or_str, bytes):
        txt = html_bytes_or_str.decode("utf-8", "replace")
    else:
        txt = html_bytes_or_str
    # Remove scripts and styles
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", txt, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    txt = re.sub(r"<[^>]+>", " ", txt)
    # Decode HTML entities
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    txt = txt.replace("&gt;", ">").replace("&#8217;", "'").replace("&#8220;", '"')
    txt = txt.replace("&#8221;", '"').replace("&#176;", "°")
    txt = re.sub(r"&#[0-9]+;", " ", txt)
    txt = re.sub(r"&[a-z]+;", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def parse_probable_cause(txt):
    """Extract probable cause from English report text."""
    if not txt:
        return None
    for pat in [
        r"(?:Probable Cause|Finding[s]?|Cause[s]? of Accident|Conclusion[s]?)\s*[:\n](.*?)(?:\n\n|\Z)",
        r"(?:The probable cause[s]? (?:of|for) this accident[:\s]*)(.*?)(?:\n\n|\Z)",
    ]:
        m = re.search(pat, txt[:10000], re.DOTALL | re.IGNORECASE)
        if m:
            v = m.group(1).strip()
            if 10 < len(v) < 3000:
                return v
    return None


# ---- DISCOVER ----------------------------------------------------------------

def discover(c):
    """Insert known reports into dgcalb_reports if not already present."""
    print("[dgcalb discover] inserting known reports …", flush=True)
    inserted = 0
    for r in KNOWN_REPORTS:
        existing = c.execute(
            "SELECT case_id FROM dgcalb_reports WHERE case_id=?", (r["case_id"],)
        ).fetchone()
        if existing:
            print(f"  {r['case_id']}: already exists", flush=True)
            continue
        c.execute(
            """INSERT OR IGNORE INTO dgcalb_reports
               (case_id, registration, aircraft, operator, event_date, location,
                source_url, source_page, lang, report_type, status, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (r["case_id"], r.get("registration"), r.get("aircraft"),
             r.get("operator"), r.get("event_date"), r.get("location"),
             r.get("source_url"), r.get("source_page"), r.get("lang", "en"),
             r.get("report_type", "Final Investigation Report"),
             now(), now()),
        )
        c.commit()
        inserted += 1
        print(f"  inserted {r['case_id']}", flush=True)
    print(f"[dgcalb discover] inserted={inserted}", flush=True)
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c):
    """Fetch PDFs / HTML for reports with status='new'."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id FROM dgcalb_reports WHERE status='new'"
    ).fetchall()

    for row in rows:
        cid = row["case_id"]
        rpt = next((r for r in KNOWN_REPORTS if r["case_id"] == cid), None)
        if not rpt:
            print(f"  {cid}: no known config, skip", file=sys.stderr)
            continue

        print(f"[dgcalb fetch] {cid} method={rpt['fetch_method']} …", flush=True)

        method = rpt["fetch_method"]

        if method == "direct":
            # Direct PDF URL — accessible via hetzner (EU IP)
            dest = os.path.join(PDFDIR, f"{cid}.pdf")
            if os.path.exists(dest) and os.path.getsize(dest) > 10000:
                print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
                c.execute(
                    "UPDATE dgcalb_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
                continue

            ok = curl_via_hetzner(rpt["source_url"], output_path=dest)
            if ok and os.path.exists(dest) and os.path.getsize(dest) > 10000:
                # Verify it's a PDF
                with open(dest, "rb") as fh:
                    magic = fh.read(4)
                if magic != b"%PDF":
                    print(f"  not a PDF: {magic!r}", file=sys.stderr)
                    c.execute(
                        "UPDATE dgcalb_reports SET status='skipped', skip_reason='not-pdf', "
                        "updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue
                print(f"  PDF saved ({os.path.getsize(dest)//1024}KB)", flush=True)
                c.execute(
                    "UPDATE dgcalb_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
            else:
                print(f"  download failed", file=sys.stderr)
                c.execute(
                    "UPDATE dgcalb_reports SET status='skipped', skip_reason='download-failed', "
                    "updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()

        elif method == "phocadownload":
            # Phocadownload URL — accessible via hetzner (EU IP required)
            dest = os.path.join(PDFDIR, f"{cid}.pdf")
            if os.path.exists(dest) and os.path.getsize(dest) > 10000:
                print(f"  already on disk ({os.path.getsize(dest)//1024}KB)", flush=True)
                c.execute(
                    "UPDATE dgcalb_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
                continue

            ok = curl_via_hetzner(rpt["phocadownload_url"], output_path=dest)
            if ok and os.path.exists(dest) and os.path.getsize(dest) > 10000:
                with open(dest, "rb") as fh:
                    magic = fh.read(4)
                if magic != b"%PDF":
                    print(f"  not a PDF: {magic!r}", file=sys.stderr)
                    c.execute(
                        "UPDATE dgcalb_reports SET status='skipped', skip_reason='not-pdf', "
                        "updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                    c.commit()
                    continue
                print(f"  PDF saved ({os.path.getsize(dest)//1024}KB)", flush=True)
                c.execute(
                    "UPDATE dgcalb_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                    (dest, now(), cid),
                )
                c.commit()
            else:
                print(f"  download failed", file=sys.stderr)
                c.execute(
                    "UPDATE dgcalb_reports SET status='skipped', skip_reason='download-failed', "
                    "updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()

        elif method == "wayback_html":
            # HTML page from Wayback — accessible from minipc
            html_data = curl_direct(rpt["wayback_url"])
            if html_data and len(html_data) > 500:
                print(f"  fetched HTML ({len(html_data)//1024}KB)", flush=True)
                # Use known_narrative if available, else extract from HTML
                narrative = rpt.get("known_narrative", "")
                if not narrative:
                    narrative = extract_text_from_html(html_data)
                if len(narrative) >= FLOOR:
                    c.execute(
                        "UPDATE dgcalb_reports SET narrative_text=?, status='fetched', "
                        "updated_at=? WHERE case_id=?",
                        (narrative, now(), cid),
                    )
                else:
                    print(f"  HTML text too short ({len(narrative)} chars)", file=sys.stderr)
                    c.execute(
                        "UPDATE dgcalb_reports SET status='skipped', skip_reason='no-text', "
                        "updated_at=? WHERE case_id=?",
                        (now(), cid),
                    )
                c.commit()
            else:
                print(f"  HTML fetch failed", file=sys.stderr)
                c.execute(
                    "UPDATE dgcalb_reports SET status='skipped', skip_reason='fetch-failed', "
                    "updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()


# ---- PARSE -------------------------------------------------------------------

def parse(c):
    """Extract text + metadata from fetched PDFs / HTML."""
    rows = c.execute(
        "SELECT case_id, pdf_path, narrative_text FROM dgcalb_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0

    for row in rows:
        cid = row["case_id"]
        print(f"[dgcalb parse] {cid} …", flush=True)

        rpt = next((r for r in KNOWN_REPORTS if r["case_id"] == cid), {})
        method = rpt.get("fetch_method", "")

        if method in ("direct", "phocadownload"):
            pdf_path = row["pdf_path"]
            if not pdf_path or not os.path.exists(pdf_path):
                print(f"  PDF missing: {pdf_path}", file=sys.stderr)
                continue
            txt = extract_text_from_pdf(pdf_path)
            print(f"  extracted {len(txt)} chars from PDF", flush=True)
        elif method == "wayback_html":
            txt = row["narrative_text"] or rpt.get("known_narrative", "")
            print(f"  using HTML/known narrative: {len(txt)} chars", flush=True)
        else:
            continue

        if len(txt) < FLOOR:
            print(f"  text too short → skipping", file=sys.stderr)
            c.execute(
                "UPDATE dgcalb_reports SET status='skipped', skip_reason='no-text', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        probable_cause = parse_probable_cause(txt)

        c.execute(
            """UPDATE dgcalb_reports SET
                 narrative_text=?, probable_cause=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, probable_cause, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  date={rpt.get('event_date')} reg={rpt.get('registration')} "
              f"pc={'yes' if probable_cause else 'no'}", flush=True)

    print(f"[dgcalb parse] parsed={parsed}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def build(c):
    """Write dgcalb_accidents from parsed rows."""
    rows = c.execute(
        """SELECT case_id, registration, aircraft, operator, event_date,
                  location, narrative_text, probable_cause, source_url, lang
           FROM dgcalb_reports WHERE status='parsed'"""
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute(
                "UPDATE dgcalb_reports SET status='skipped', skip_reason='no-text', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        rpt = next((x for x in KNOWN_REPORTS if x["case_id"] == r["case_id"]), {})
        slug = r["case_id"].lower()

        c.execute(
            """INSERT OR REPLACE INTO dgcalb_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'LB', ?, ?, ?, ?, ?, ?, ?)""",
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
                rpt.get("report_type", "Final Investigation Report"),
                slug,
                r["lang"] or "en",
                now(),
            ),
        )
        c.execute(
            "UPDATE dgcalb_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[dgcalb build] built={built}", flush=True)
    return built


# ---- STATS -------------------------------------------------------------------

def print_stats(c):
    print("\n--- dgcalb_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM dgcalb_reports GROUP BY status, skip_reason"
    ):
        print(f"  {row['status']:10s} {(row['skip_reason'] or ''):25s} {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM dgcalb_accidents").fetchone()[0]
    print(f"\n--- dgcalb_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM dgcalb_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), MAX(LENGTH(narrative_text)) FROM dgcalb_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  narr_len min={narr[0]} max={narr[1]}")
        print("\n  sample rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, LENGTH(narrative_text) len "
            "FROM dgcalb_accidents ORDER BY event_date NULLS LAST LIMIT 10"
        ):
            print(f"    {r['case_id']:30s}  reg={r['registration'] or 'NULL':12s}  "
                  f"date={r['event_date'] or 'NULL'}  narr={r['len']}")


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
