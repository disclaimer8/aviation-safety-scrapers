#!/usr/bin/env python3
"""daaina (Namibia DAAI — Directorate of Aircraft Accident Investigations, country NA)
aviation-accident ingest.

Source: mwt.gov.na/published-daai-report (Liferay CMS)
The live site is geo-fenced/offline; all content is fetched from Wayback Machine.

STRATEGY:
  1. Fetch all available Wayback snapshots of /published-daai-report listing page
  2. Parse the HTML table to extract metadata (date, aircraft, reg, report_type)
     and Liferay document paths
  3. CDX-check each PDF path individually (prefix scan returns empty for
     Liferay /documents/ paths — use exact URL CDX lookup instead)
  4. Download best (max-length) Wayback capture of each PDF via id_ URL
     Trap: Wayback truncates at 1,048,576 bytes — pick largest non-truncated capture
  5. Extract text via pdftotext; OCR via OCR_REMOTE for scanned PDFs
  6. Build daaina_accidents table

FETCH TRAP: minipc->archive.org intermittent Connection refused.
  PDF downloads are performed via hetzner SSH (FETCH_VIA_SSH env var).
  Set FETCH_VIA_SSH=<ocr-host> to enable remote-fetch.

SUPERSESSION: If both PRELIMINARY and FINAL exist for same registration+event_date,
              keep FINAL and mark PRELIMINARY as superseded.

case_id: 'daaina-' + registration.lower() (Namibian regs V5-XXX)
         for foreign-reg events: 'daaina-' + reg.lower()
         for no-reg events: 'daaina-' + aircraft_slug + '-' + YYYYMMDD

Liferay document URL format:
  /documents/<groupId>/<folderId>/<filename>/<uuid>
  groupId=576663, folderId=1344073

WAYBACK TRAPS:
  - CDX prefix scan for /documents/ path returns [] -- use exact URL CDX lookup
  - Normal Wayback proxy (without id_) follows redirect to best capture
  - id_ modifier requires the exact timestamp from CDX
  - Truncation at exactly 1,048,576 bytes -- pick capture with length != 1048576
  - PDFs must be fetched via hetzner (minipc->archive.org Connection refused)
"""

import sys
import os
import re
import time
import sqlite3
import subprocess
import json
import urllib.parse
import urllib.request

MWT_HOST = "mwt.gov.na"
LISTING_URL = f"https://{MWT_HOST}/published-daai-report"
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

# Known good listing snapshots (all have >=25 entries; 2023-01-31 has 38)
LISTING_SNAPSHOTS = [
    "20210608105010",
    "20210928195012",
    "20220330035016",
    "20230131165816",
]

DELAY = 2.5          # seconds between requests
FLOOR = 300          # min narrative chars to accept into accidents table
OCR_FLOOR = 200      # chars below which we attempt OCR
HOME = os.path.expanduser("~/daaina-ingest")
DB = os.path.join(HOME, "daaina.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
WAYBACK_TRUNCATION = 1_048_576  # Wayback truncates at exactly 1MB

# Fetch PDFs via hetzner SSH to avoid minipc->archive.org Connection refused
FETCH_VIA_SSH = os.environ.get("FETCH_VIA_SSH", "")

# Namibian registration pattern
_REG_NA = re.compile(r'\bV5-[A-Z0-9]{2,3}\b', re.IGNORECASE)
_REG_ANY = re.compile(
    r'\b(V5-[A-Z0-9]{2,3}|ZS-[A-Z]{3}|ZT-[A-Z]{3}|C9-[A-Z]{3}|[A-Z]{1,2}-[A-Z]{3,5}|N\d{3,5}[A-Z]{0,2})\b',
    re.IGNORECASE,
)
_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
_DATE_LONG = re.compile(
    r'\b(\d{1,2})\s+(' + '|'.join(_MONTHS.keys()) + r')\s+(\d{4})\b',
    re.IGNORECASE,
)
_DATE_ISO = re.compile(r'\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b')


SCHEMA = """
CREATE TABLE IF NOT EXISTS daaina_reports (
    case_id        TEXT PRIMARY KEY,
    registration   TEXT,
    aircraft       TEXT,
    event_date     TEXT,
    report_type    TEXT,
    source_url     TEXT,
    archive_url    TEXT,
    archive_ts     TEXT,
    archive_len    INT,
    pdf_path       TEXT,
    narrative_text TEXT,
    probable_cause TEXT,
    operator       TEXT,
    location       TEXT,
    lang           TEXT DEFAULT 'en',
    status         TEXT DEFAULT 'new',
    skip_reason    TEXT,
    superseded_by  TEXT,
    discovered_at  INT,
    updated_at     INT
);
CREATE TABLE IF NOT EXISTS daaina_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'NA',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT DEFAULT 'Final Report',
    site_slug      TEXT,
    lang           TEXT DEFAULT 'en',
    built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_daaina_status ON daaina_reports(status);
CREATE INDEX IF NOT EXISTS idx_daaina_reg    ON daaina_reports(registration);
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


def _urlopen(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def cdx_best(orig_url, retries=3):
    """Return (timestamp, length) of best (largest 200) Wayback capture.

    NOTE: CDX prefix scan for /documents/ paths returns empty.
    Must use exact URL lookup.
    """
    params = urllib.parse.urlencode({
        "url": orig_url,
        "output": "json",
        "fl": "timestamp,statuscode,length",
        "filter": "statuscode:200",
        "limit": "10",
    })
    cdx_url = f"{CDX_BASE}?{params}"
    for attempt in range(retries):
        try:
            resp = _urlopen(cdx_url, timeout=20)
            data = json.loads(resp.read())
            time.sleep(DELAY)
            if len(data) <= 1:
                return None, 0
            rows = data[1:]
            # Prefer largest capture that is NOT exactly 1MB (truncated)
            non_trunc = [r for r in rows if int(r[2] or 0) != WAYBACK_TRUNCATION]
            pool = non_trunc if non_trunc else rows
            best = max(pool, key=lambda r: int(r[2]) if r[2].isdigit() else 0)
            return best[0], int(best[2]) if best[2].isdigit() else 0
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
            else:
                print(f"  [cdx] error for {orig_url}: {e}", file=sys.stderr)
    return None, 0


def _curl_fetch(url, dest_path, timeout=90):
    """Fetch URL to dest_path using curl. Returns (success, size)."""
    cmd = [
        "curl", "-sL",
        "--max-time", str(timeout),
        "--connect-timeout", "8",
        "-A", UA,
        "-o", dest_path,
        "-w", "%{http_code}",
        url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        http_code = result.stdout.strip()
        if http_code == "200" and os.path.exists(dest_path):
            size = os.path.getsize(dest_path)
            return True, size
        return False, 0
    except Exception as e:
        print(f"  curl error: {e}", file=sys.stderr)
        return False, 0


def _ssh_fetch(ssh_host, url, dest_path, timeout=90):
    """Fetch URL via SSH hop (for when local curl can't reach archive.org).
    Downloads to remote /tmp, then scp back.
    """
    remote_tmp = f"/tmp/daaina_fetch_{os.getpid()}.pdf"
    cmd = [
        "ssh", ssh_host,
        f"curl -sL --max-time {timeout} --connect-timeout 8 "
        f"-A '{UA}' -o {remote_tmp} -w '%{{http_code}}' '{url}'"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20)
        http_code = result.stdout.strip()
        if http_code == "200":
            scp_result = subprocess.run(
                ["scp", "-q", f"{ssh_host}:{remote_tmp}", dest_path],
                capture_output=True, timeout=60,
            )
            subprocess.run(
                ["ssh", ssh_host, f"rm -f {remote_tmp}"],
                capture_output=True, timeout=10,
            )
            if scp_result.returncode == 0 and os.path.exists(dest_path):
                return True, os.path.getsize(dest_path)
        return False, 0
    except Exception as e:
        print(f"  ssh-fetch error: {e}", file=sys.stderr)
        return False, 0


def wayback_fetch_pdf(orig_url, ts, dest_path, retries=3):
    """Download PDF from Wayback Machine."""
    archive_url = f"{WAYBACK_BASE}/{ts}id_/{orig_url}"
    for attempt in range(retries):
        print(f"  [fetch] attempt {attempt+1} url={archive_url[:80]}…", flush=True)
        # Use SSH hop if configured (minipc->archive.org is unreliable)
        if FETCH_VIA_SSH:
            ok, size = _ssh_fetch(FETCH_VIA_SSH, archive_url, dest_path)
        else:
            ok, size = _curl_fetch(archive_url, dest_path)

        if not ok:
            if attempt < retries - 1:
                time.sleep(20)
            continue

        # Validate PDF header
        with open(dest_path, "rb") as f:
            header = f.read(4)
        if header != b"%PDF":
            print(f"  not PDF (first bytes: {header!r})", file=sys.stderr)
            os.remove(dest_path)
            if attempt < retries - 1:
                time.sleep(10)
            continue

        time.sleep(DELAY)
        return True, archive_url

    return False, archive_url


# ---- DISCOVER ----------------------------------------------------------------

def _parse_listing_html(html):
    """Parse the published-daai-report HTML table."""
    entries = []
    rows = re.findall(r'<tr[^>]*>.*?</tr>', html, re.S)
    for row in rows:
        pdfs = re.findall(
            r'href=["\'](/documents/576663/1344073/[^"\']+\.pdf[^"\']*)["\']',
            row,
            re.IGNORECASE,
        )
        if not pdfs:
            continue
        # Clean row text
        text = re.sub(r'<[^>]+>', ' ', row)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&#\d+;', '', text)
        text = re.sub(r'&[a-z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # Parse report_type
        report_type = 'FINAL'
        if re.search(r'\bPRELIM', text, re.I):
            report_type = 'PRELIMINARY'
        elif re.search(r'\bINTERIM\b', text, re.I):
            report_type = 'INTERIM'

        # Parse date from row text
        event_date = None
        m = _DATE_LONG.search(text)
        if m:
            d, mon_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
            mo = next((v for k, v in _MONTHS.items() if mon_str.startswith(k)), None)
            if mo and 1990 <= y <= 2030:
                event_date = f"{y:04d}-{mo:02d}-{d:02d}"

        # Parse registration from anchor text or row
        reg = None
        anchor_regs = re.findall(r'target[^>]*>([^<]*)</a>', row)
        for at in anchor_regs:
            m = _REG_ANY.search(at)
            if m:
                reg = m.group(1).upper()
                break
        if not reg:
            m = _REG_NA.search(text)
            reg = m.group(0).upper() if m else None
        if not reg:
            m = _REG_ANY.search(text)
            reg = m.group(1).upper() if m else None

        # Parse aircraft type
        aircraft = None
        ac_m = re.search(
            r'(?:Accident|Incident|Serious Incident)\s+(.+?)\s+(?:V5-|ZS-|ZT-|C9-|N\d|NONE|\w{2}-)',
            text, re.I,
        )
        if ac_m:
            aircraft = re.sub(r'\s+', ' ', ac_m.group(1).strip())[:60]

        for doc_path in pdfs:
            entries.append({
                'event_date': event_date,
                'text': text,
                'aircraft': aircraft,
                'registration': reg,
                'report_type': report_type,
                'doc_path': doc_path,
            })
    return entries


def discover(c):
    """Fetch all listing snapshots from Wayback and insert report records."""
    print("[daaina discover] fetching listing snapshots …", flush=True)
    all_entries = {}  # keyed by filename (without UUID)

    for ts in LISTING_SNAPSHOTS:
        wb_url = f"{WAYBACK_BASE}/{ts}id_/{LISTING_URL}"
        print(f"  snapshot {ts} …", flush=True)
        try:
            resp = _urlopen(wb_url, timeout=30)
            html = resp.read().decode("utf-8", "replace")
            time.sleep(DELAY)
            entries = _parse_listing_html(html)
            print(f"    parsed {len(entries)} entries", flush=True)
            for e in entries:
                # filename before UUID as dedup key
                fn_key = e['doc_path'].split('/')[-2].lower()
                existing = all_entries.get(fn_key)
                if not existing:
                    all_entries[fn_key] = e
                else:
                    # Prefer FINAL > INTERIM > PRELIMINARY
                    priority = {'FINAL': 2, 'INTERIM': 1, 'PRELIMINARY': 0}
                    if priority.get(e['report_type'], 0) > priority.get(existing['report_type'], 0):
                        all_entries[fn_key] = e
        except Exception as ex:
            print(f"  ERROR snapshot {ts}: {ex}", file=sys.stderr)

    print(f"  {len(all_entries)} unique PDF paths", flush=True)

    inserted = 0
    for fn_key, e in sorted(all_entries.items()):
        reg = e['registration'] or ''
        doc_path = e['doc_path']

        # Build case_id from registration if available, else filename
        # Using reg-based IDs for final reports, fn-based for prelim/no-reg
        if reg and e['report_type'] == 'FINAL':
            base = re.sub(r'[^A-Za-z0-9]', '-', reg).lower()
        else:
            base = re.sub(r'[^A-Za-z0-9]', '-', fn_key.replace('.pdf', '')).lower().strip('-')[:30]
        cid = 'daaina-' + base

        # Ensure uniqueness by adding numeric suffix if collision
        orig_cid = cid
        for suffix in range(1, 10):
            chk = c.execute("SELECT case_id FROM daaina_reports WHERE case_id=?", (cid,)).fetchone()
            if not chk:
                break
            cid = orig_cid + f'-{suffix}'

        orig_url = f"https://{MWT_HOST}{doc_path}"
        c.execute(
            "INSERT OR IGNORE INTO daaina_reports "
            "(case_id, registration, aircraft, event_date, report_type, "
            " source_url, lang, status, discovered_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'en', 'new', ?, ?)",
            (cid, reg or None, e['aircraft'], e['event_date'],
             e['report_type'], orig_url, now(), now()),
        )
        c.commit()
        inserted += 1

    print(f"[daaina discover] inserted {inserted} new records", flush=True)
    return inserted


# ---- FETCH -------------------------------------------------------------------

def fetch(c):
    """CDX-check each PDF and download from Wayback."""
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, source_url FROM daaina_reports WHERE status='new'"
    ).fetchall()
    downloaded = 0

    for row in rows:
        cid = row["case_id"]
        orig_url = row["source_url"]
        safe_name = re.sub(r"[^A-Za-z0-9_.\-]", "_", cid) + ".pdf"
        dest = os.path.join(PDFDIR, safe_name)

        print(f"[daaina fetch] {cid} …", flush=True)

        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            size = os.path.getsize(dest)
            print(f"  already on disk ({size//1024}KB)", flush=True)
            c.execute(
                "UPDATE daaina_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            downloaded += 1
            continue

        ts, cdx_len = cdx_best(orig_url)
        if not ts:
            print(f"  not in Wayback CDX", flush=True)
            c.execute(
                "UPDATE daaina_reports SET status='skipped', skip_reason='not-archived', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        print(f"  ts={ts} cdx_len={cdx_len}", flush=True)
        ok, archive_url = wayback_fetch_pdf(orig_url, ts, dest, retries=3)

        if not ok:
            if os.path.exists(dest):
                os.remove(dest)
            c.execute(
                "UPDATE daaina_reports SET status='skipped', skip_reason='fetch-failed', "
                "updated_at=? WHERE case_id=?",
                (now(), cid),
            )
            c.commit()
            continue

        size = os.path.getsize(dest)
        if size == WAYBACK_TRUNCATION:
            print(f"  WARNING: file is exactly 1MB (possible truncation)", flush=True)

        c.execute(
            "UPDATE daaina_reports SET pdf_path=?, archive_url=?, archive_ts=?, "
            "archive_len=?, status='fetched', updated_at=? WHERE case_id=?",
            (dest, archive_url, ts, cdx_len, now(), cid),
        )
        c.commit()
        downloaded += 1
        print(f"  saved {size//1024}KB", flush=True)

    print(f"[daaina fetch] downloaded={downloaded}", flush=True)
    return downloaded


# ---- PARSE -------------------------------------------------------------------

def _run_pdftotext(pdf_path):
    try:
        cp = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=120,
        )
        return cp.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print(f"  pdftotext error: {e}", file=sys.stderr)
        return ""


def _run_ocr(pdf_path):
    """Run OCR via OCR_REMOTE (hetzner) or local fallback."""
    ocr_remote = os.environ.get("OCR_REMOTE", "")
    if ocr_remote:
        remote_in = "/tmp/ocr_in_daaina.pdf"
        remote_out = "/tmp/ocr_out_daaina.pdf"
        local_out = pdf_path.replace(".pdf", "_ocr.pdf")
        try:
            subprocess.run(["scp", "-q", pdf_path, f"{ocr_remote}:{remote_in}"],
                           check=True, timeout=60)
            subprocess.run(
                ["ssh", ocr_remote,
                 f"nice -n 19 ionice -c 3 ocrmypdf --force-ocr -l eng "
                 f"{remote_in} {remote_out}"],
                check=True, timeout=300,
            )
            subprocess.run(["scp", "-q", f"{ocr_remote}:{remote_out}", local_out],
                           check=True, timeout=60)
            return _run_pdftotext(local_out)
        except Exception as e:
            print(f"  OCR_REMOTE error: {e}", file=sys.stderr)
    # Local fallback
    try:
        local_out = pdf_path.replace(".pdf", "_ocr.pdf")
        subprocess.run(
            ["ocrmypdf", "--force-ocr", "-l", "eng", pdf_path, local_out],
            check=True, timeout=300, capture_output=True,
        )
        return _run_pdftotext(local_out)
    except Exception as e:
        print(f"  local OCR error: {e}", file=sys.stderr)
    return ""


def _parse_date(txt, hint=None):
    if hint:
        return hint
    if not txt:
        return None
    header = txt[:8000]
    m = _DATE_LONG.search(header)
    if m:
        d, mon_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = next((v for k, v in _MONTHS.items() if mon_str.startswith(k)), None)
        if mo and 1990 <= y <= 2030:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _DATE_ISO.search(header)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1990 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _parse_reg(txt, hint=None):
    if hint:
        return hint
    if not txt:
        return None
    header = txt[:5000]
    m = _REG_NA.search(header)
    if m:
        return m.group(0).upper()
    m = _REG_ANY.search(header)
    return m.group(1).upper() if m else None


def _parse_aircraft(txt):
    if not txt:
        return None
    header = txt[:6000]
    for pat in [
        r'(?:Aircraft\s+(?:type|make)?[:\s]+|Type\s+of\s+aircraft[:\s]+)([A-Za-z0-9][\w /\-]{2,50})',
        r'\b(Cessna\s+[\w\-]+|Robinson\s+R\d+|Bell\s+\d+|Piper\s+[\w\-]+|Beechcraft?\s+[\w\-]+|Boeing\s+[\w\-]+|Airbus\s+[A-Z]\d[\w\-]+|Maule\s+[\w\-]+|Jabiru\s+[\w\-]+|Hughes\s+\d+|Schweizer\s+[\w\-]+|McDonnell\s+Douglas\s+[\w\-]+|ERJ-\d+)',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            if 2 < len(v) < 70:
                return v
    return None


def _parse_location(txt):
    if not txt:
        return None
    header = txt[:6000]
    for pat in [
        r'(?:Location[:\s]+|Place\s+of\s+(?:accident|occurrence)[:\s]+|Site\s+of\s+accident[:\s]+)([A-Za-z][\w ,.\-]{3,80})',
        r'(?:occurred\s+(?:at|near|over|in)\s+)([A-Za-z][\w ,.\-]{3,80})',
        r'(?:near|at)\s+([A-Z][a-z][A-Za-z\s]{2,40}),\s*Namibia',
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
            if 3 < len(v) < 100:
                return v
    return None


def _parse_operator(txt):
    if not txt:
        return None
    header = txt[:6000]
    m = re.search(r'(?:Operator|Owner)[:\s]+([A-Za-z][\w /\-,.]{3,80})',
                  header, re.IGNORECASE)
    if m:
        v = re.split(r'[\n\r]', m.group(1))[0].strip().strip(',;.')
        if 3 < len(v) < 100:
            return v
    return None


def _parse_probable_cause(txt):
    if not txt:
        return None
    m = re.search(
        r'(?:Probable\s+Cause[s]?|Findings|Cause\s+of\s+(?:Accident|Incident))[:\s]*\n?(.*?)(?:\n{2,}|\Z)',
        txt, re.DOTALL | re.IGNORECASE,
    )
    if m:
        v = m.group(1).strip()
        if 10 < len(v) < 4000:
            return v
    return None


def parse(c):
    """Extract text and metadata from fetched PDFs."""
    rows = c.execute(
        "SELECT case_id, pdf_path, source_url, registration, aircraft, "
        "       event_date, report_type "
        "FROM daaina_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    ocr_queue = []

    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        print(f"[daaina parse] {cid}", flush=True)

        if not pdf_path or not os.path.exists(pdf_path):
            print(f"  PDF missing", file=sys.stderr)
            continue

        raw_txt = _run_pdftotext(pdf_path)
        size_kb = os.path.getsize(pdf_path) // 1024
        print(f"  pdftotext => {len(raw_txt)} chars (file={size_kb}KB)", flush=True)

        if len(raw_txt) < OCR_FLOOR:
            ocr_queue.append(cid)
            print(f"  short text => queuing for OCR", flush=True)
            c.execute(
                "UPDATE daaina_reports SET narrative_text=?, status='needs-ocr', "
                "updated_at=? WHERE case_id=?",
                (raw_txt, now(), cid),
            )
            c.commit()
            continue

        event_date = _parse_date(raw_txt, row["event_date"])
        reg = _parse_reg(raw_txt, row["registration"])
        aircraft = row["aircraft"] or _parse_aircraft(raw_txt)
        location = _parse_location(raw_txt)
        operator = _parse_operator(raw_txt)
        probable_cause = _parse_probable_cause(raw_txt)

        c.execute(
            """UPDATE daaina_reports SET
                 narrative_text=?, probable_cause=?, event_date=?,
                 registration=?, aircraft=?, location=?, operator=?,
                 status='parsed', updated_at=?
               WHERE case_id=?""",
            (raw_txt, probable_cause, event_date, reg, aircraft,
             location, operator, now(), cid),
        )
        c.commit()
        parsed += 1
        print(f"  date={event_date} reg={reg} chars={len(raw_txt)}", flush=True)

    # OCR pass
    if ocr_queue:
        print(f"[daaina parse] OCR on {len(ocr_queue)} PDFs …", flush=True)
        for cid in ocr_queue:
            row = c.execute(
                "SELECT pdf_path, registration, aircraft, event_date "
                "FROM daaina_reports WHERE case_id=?", (cid,)
            ).fetchone()
            if not row or not row["pdf_path"]:
                continue
            print(f"  OCR {cid} …", flush=True)
            ocr_txt = _run_ocr(row["pdf_path"])
            print(f"    OCR => {len(ocr_txt)} chars", flush=True)

            if len(ocr_txt) < 50:
                c.execute(
                    "UPDATE daaina_reports SET status='skipped', "
                    "skip_reason='ocr-failed', updated_at=? WHERE case_id=?",
                    (now(), cid),
                )
                c.commit()
                continue

            event_date = _parse_date(ocr_txt, row["event_date"])
            reg = _parse_reg(ocr_txt, row["registration"])
            aircraft = row["aircraft"] or _parse_aircraft(ocr_txt)
            location = _parse_location(ocr_txt)
            operator = _parse_operator(ocr_txt)
            probable_cause = _parse_probable_cause(ocr_txt)

            c.execute(
                """UPDATE daaina_reports SET
                     narrative_text=?, probable_cause=?, event_date=?,
                     registration=?, aircraft=?, location=?, operator=?,
                     status='parsed', updated_at=?
                   WHERE case_id=?""",
                (ocr_txt, probable_cause, event_date, reg, aircraft,
                 location, operator, now(), cid),
            )
            c.commit()
            parsed += 1

    print(f"[daaina parse] parsed={parsed} ocr_queued={len(ocr_queue)}", flush=True)
    return parsed


# ---- BUILD -------------------------------------------------------------------

def _mark_supersession(c):
    """Mark PRELIMINARY/INTERIM as superseded when FINAL exists for same reg+date."""
    prelims = c.execute(
        "SELECT case_id, registration, event_date FROM daaina_reports "
        "WHERE status='parsed' AND report_type IN ('PRELIMINARY','INTERIM') "
        "AND superseded_by IS NULL"
    ).fetchall()
    for row in prelims:
        if not row["registration"] or not row["event_date"]:
            continue
        final = c.execute(
            "SELECT case_id FROM daaina_reports "
            "WHERE registration=? AND event_date=? AND report_type='FINAL' "
            "AND status='parsed'",
            (row["registration"], row["event_date"]),
        ).fetchone()
        if final:
            c.execute(
                "UPDATE daaina_reports SET superseded_by=? WHERE case_id=?",
                (final["case_id"], row["case_id"]),
            )
            c.commit()
            print(f"  superseded: {row['case_id']} => {final['case_id']}", flush=True)


def build(c):
    """Write daaina_accidents from parsed rows."""
    _mark_supersession(c)

    rows = c.execute(
        """SELECT case_id, registration, aircraft, event_date, report_type,
                  operator, location, narrative_text, probable_cause, source_url
           FROM daaina_reports
           WHERE status='parsed' AND superseded_by IS NULL"""
    ).fetchall()
    built = 0

    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            print(f"  [build] skip {r['case_id']}: narrative {len(narr)} < {FLOOR} chars", flush=True)
            c.execute(
                "UPDATE daaina_reports SET status='skipped', skip_reason='short-narrative', "
                "updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            continue

        cid = r["case_id"]
        report_label = {
            'FINAL': 'Final Report',
            'PRELIMINARY': 'Preliminary Report',
            'INTERIM': 'Interim Report',
        }.get(r["report_type"], r["report_type"] or 'Final Report')

        c.execute(
            """INSERT OR REPLACE INTO daaina_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?, ?, ?, ?, ?, ?, 'NA', ?, ?, ?, ?, ?, 'en', ?)""",
            (
                cid, r["event_date"], r["aircraft"], r["registration"],
                r["operator"], r["location"], narr, r["probable_cause"],
                r["source_url"], report_label, cid.lower(), now(),
            ),
        )
        c.execute(
            "UPDATE daaina_reports SET status='built', updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1

    print(f"[daaina build] built={built}", flush=True)
    return built


# ---- STATS ------------------------------------------------------------------

def print_stats(c):
    print("\n--- daaina_reports status ---")
    for row in c.execute(
        "SELECT status, skip_reason, count(*) n FROM daaina_reports "
        "GROUP BY status, skip_reason ORDER BY n DESC"
    ):
        print(f"  {row['status']:14s} {(row['skip_reason'] or ''):25s} {row['n']}")

    cnt = c.execute("SELECT COUNT(*) FROM daaina_accidents").fetchone()[0]
    print(f"\n--- daaina_accidents: {cnt} rows ---")
    if cnt:
        null_dates = c.execute(
            "SELECT COUNT(*) FROM daaina_accidents WHERE event_date IS NULL"
        ).fetchone()[0]
        narr = c.execute(
            "SELECT MIN(LENGTH(narrative_text)), CAST(AVG(LENGTH(narrative_text)) AS INT), "
            "       MAX(LENGTH(narrative_text)) FROM daaina_accidents"
        ).fetchone()
        print(f"  event_date NULL: {null_dates}  narr_len min={narr[0]} avg={narr[1]} max={narr[2]}")
        print("\n  all rows:")
        for r in c.execute(
            "SELECT case_id, registration, event_date, report_type, LENGTH(narrative_text) len "
            "FROM daaina_accidents ORDER BY event_date NULLS LAST"
        ):
            print(f"    {r['case_id']:35s}  reg={r['registration'] or 'NULL':10s}  "
                  f"date={r['event_date'] or 'NULL'}  {r['report_type']:18s}  narr={r['len']}")

    skipped = c.execute(
        "SELECT case_id, skip_reason FROM daaina_reports WHERE status='skipped'"
    ).fetchall()
    if skipped:
        print(f"\n  skipped ({len(skipped)}):")
        for r in skipped:
            print(f"    {r['case_id']}  {r['skip_reason']}")

    prelims = c.execute(
        "SELECT case_id, superseded_by FROM daaina_reports WHERE superseded_by IS NOT NULL"
    ).fetchall()
    if prelims:
        print(f"\n  superseded ({len(prelims)}):")
        for r in prelims:
            print(f"    {r['case_id']} => {r['superseded_by']}")


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
