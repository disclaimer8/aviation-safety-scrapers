# eaaid_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for EAAID (Egypt).

Fetching strategy:
  - All PDFs must be fetched from Hetzner (site times out from Mac/minipc).
  - We use sequential SSH+curl calls via hetzner, saving to /tmp on hetzner,
    then rsync to minipc pdfs/ directory.
  - Cookie: acquired fresh from the listing page on each run; stored in DB as
    'session_cookie' meta row.

Stages:
  discover  — fetch listing HTML from hetzner, parse, insert new rows
  fetch     — download PDFs via hetzner SSH+curl, rsync to pdfs/
  parse     — extract text, strip Arabic, classify tier
  build     — emit eaaid_accidents rows
  all       — discover + fetch + parse + build
"""
import os
import re
import subprocess
import sys
import time

from . import db, eaaid
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

HETZNER_HOST = "user@prod.example"
REMOTE_TMP   = "/tmp/eaaid-pdfs"
FETCH_DELAY  = 2.0   # seconds between individual PDF downloads on hetzner


def _hetzner_run(cmd, **kwargs):
    """Run command on hetzner, return CompletedProcess."""
    return subprocess.run(["ssh", HETZNER_HOST] + cmd, **kwargs)


def _acquire_cookie(timeout=60):
    """GET listing page from hetzner, return session cookie string."""
    result = subprocess.run(
        ["ssh", HETZNER_HOST,
         f"curl -s --max-time {timeout} -c /tmp/eaaid_cookies.txt -b /tmp/eaaid_cookies.txt "
         f"-A '{eaaid.UA}' -o /dev/null -w '%{{http_code}}' "
         f"'{eaaid.LISTING_URL}'"],
        capture_output=True, timeout=timeout + 10,
    )
    code = result.stdout.decode().strip()
    print(f"[eaaid discover] listing probe: HTTP {code}")
    # Read cookie value
    cookie_result = subprocess.run(
        ["ssh", HETZNER_HOST, "grep -o 'cookiesession1[^\t]*' /tmp/eaaid_cookies.txt 2>/dev/null || true"],
        capture_output=True, timeout=15,
    )
    cookie_line = cookie_result.stdout.decode().strip()
    if "\t" in cookie_line:
        cookie_val = cookie_line.split("\t")[-1].strip()
    elif "=" in cookie_line:
        cookie_val = cookie_line.split("=", 1)[1].strip() if "=" in cookie_line else ""
    else:
        cookie_val = cookie_line
    cookie_str = f"cookiesession1={cookie_val}" if cookie_val else ""
    print(f"[eaaid discover] cookie: {cookie_str[:60]}...")
    return cookie_str


def discover(conn):
    """Fetch listing from hetzner, parse table, insert new rows.

    Returns (inserted, total) counts.
    """
    # Acquire session cookie
    cookie = _acquire_cookie()

    # Fetch listing HTML
    result = subprocess.run(
        ["ssh", HETZNER_HOST,
         f"curl -s --max-time 90 -b '{cookie}' "
         f"-A '{eaaid.UA}' "
         f"'{eaaid.LISTING_URL}'"],
        capture_output=True, timeout=120,
    )
    html = result.stdout.decode("utf-8", "replace")
    if "<tbody>" not in html:
        print(f"[eaaid discover] ERROR: listing page did not return expected table HTML", file=sys.stderr)
        print(f"[eaaid discover] Response snippet: {html[:500]}", file=sys.stderr)
        return 0, 0

    events = eaaid.parse_listing_html(html)
    events = eaaid.assign_case_ids(events)
    print(f"[eaaid discover] parsed {len(events)} unique events from listing")

    inserted = 0
    ts = db.now_ms()
    for evt in events:
        existing = conn.execute(
            "SELECT case_id FROM eaaid_reports WHERE guid=?", (evt["guid"],)
        ).fetchone()
        if existing:
            continue
        src_url = eaaid.source_url(evt["href"])
        conn.execute(
            "INSERT INTO eaaid_reports "
            "(case_id, guid, source_url, report_type, category, event_date, "
            "location, aircraft, registration, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                evt["case_id"],
                evt["guid"],
                src_url,
                evt["report_type"],
                evt["category"],
                evt["date"],
                evt["location"],
                evt["aircraft"],
                evt["registration"],
                "en",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    print(f"[eaaid discover] inserted {inserted} new rows")
    return inserted, len(events)


def fetch(conn, pdf_dir):
    """Download PDFs for all status='new' rows via hetzner SSH+curl.

    Strategy:
      1. Build list of (case_id, guid, source_url, dest_filename)
      2. For each: SSH curl to hetzner remote /tmp/eaaid-pdfs/<filename>
      3. Rsync the batch back to minipc
      4. Update DB
    """
    os.makedirs(pdf_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT case_id, guid, source_url FROM eaaid_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    if not rows:
        print("[eaaid fetch] no new rows to fetch")
        return 0

    print(f"[eaaid fetch] {len(rows)} PDFs to download via hetzner")

    # Ensure remote tmp dir exists
    _hetzner_run(["mkdir", "-p", REMOTE_TMP], capture_output=True, timeout=15)

    # Acquire a fresh cookie
    _hetzner_run(
        [f"curl -s --max-time 90 -c /tmp/eaaid_cookies.txt -b /tmp/eaaid_cookies.txt "
         f"-A '{eaaid.UA}' -o /dev/null '{eaaid.LISTING_URL}'"],
        capture_output=True, timeout=120,
    )

    downloads = []
    for row in rows:
        case_id   = row["case_id"]
        guid      = row["guid"]
        url       = row["source_url"]
        filename  = eaaid.pdf_filename(case_id, guid)
        remote_dest = f"{REMOTE_TMP}/{filename}"
        local_dest  = os.path.join(pdf_dir, filename)
        downloads.append((case_id, guid, url, filename, remote_dest, local_dest))

    ok_count   = 0
    fail_count = 0

    for (case_id, guid, url, filename, remote_dest, local_dest) in downloads:
        # Skip if already on disk
        if os.path.exists(local_dest) and os.path.getsize(local_dest) > 500:
            with open(local_dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    print(f"[eaaid fetch] {case_id}: already on disk, marking fetched")
                    conn.execute(
                        "UPDATE eaaid_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                        (local_dest, db.STATUS_FETCHED, db.now_ms(), case_id),
                    )
                    conn.commit()
                    ok_count += 1
                    continue

        print(f"[eaaid fetch] {case_id}: downloading via hetzner...", flush=True)
        dl_result = subprocess.run(
            ["ssh", HETZNER_HOST,
             f"curl -s --max-time 120 "
             f"-b /tmp/eaaid_cookies.txt "
             f"-A '{eaaid.UA}' "
             f"-L -o '{remote_dest}' '{url}' "
             f"&& wc -c < '{remote_dest}'"],
            capture_output=True, timeout=150,
        )
        out = dl_result.stdout.decode().strip()
        print(f"[eaaid fetch]   hetzner result: rc={dl_result.returncode} bytes={out}")
        time.sleep(FETCH_DELAY)

        if dl_result.returncode != 0:
            fail_count += 1
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, "fetch-failed", db.now_ms(), case_id),
            )
            conn.commit()
            continue

        # Rsync single file back
        rsync = subprocess.run(
            ["rsync", "-az", f"{HETZNER_HOST}:{remote_dest}", local_dest],
            capture_output=True, timeout=120,
        )
        if rsync.returncode != 0 or not os.path.exists(local_dest):
            fail_count += 1
            print(f"[eaaid fetch]   rsync failed for {case_id}: rc={rsync.returncode}")
            continue

        # Validate PDF magic
        with open(local_dest, "rb") as f:
            magic = f.read(4)
        if magic != b"%PDF":
            fail_count += 1
            print(f"[eaaid fetch]   {case_id}: not a PDF (magic={magic!r}), skipping")
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, "not-pdf", db.now_ms(), case_id),
            )
            conn.commit()
            continue

        fsize = os.path.getsize(local_dest)
        print(f"[eaaid fetch]   {case_id}: OK ({fsize} bytes)")
        conn.execute(
            "UPDATE eaaid_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
            (local_dest, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()
        ok_count += 1

    # Cleanup remote tmp
    _hetzner_run(["rm", "-rf", REMOTE_TMP], capture_output=True, timeout=30)

    print(f"[eaaid fetch] done: ok={ok_count} fail={fail_count}")
    return ok_count


def parse(conn, ocr_remote_host=None):
    """Extract text from PDFs, classify tier, advance to 'parsed'.

    If ocr_remote_host is set and a PDF is scanned (tier='scanned'),
    attempt OCR via the remote host.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM eaaid_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    parsed_count = 0
    for row in rows:
        pdf_path = row["pdf_path"]
        text = extract_text(pdf_path) if pdf_path else ""
        tier = _classify_tier(text)

        # OCR path for scanned/empty PDFs (no text layer)
        if tier in ("scanned", "none") and ocr_remote_host:
            from .pdf import ocr_remote
            print(f"[eaaid parse] {row['case_id']}: attempting OCR via {ocr_remote_host}")
            text = ocr_remote(pdf_path, ocr_remote_host)
            tier = _classify_tier(text)
            print(f"[eaaid parse] {row['case_id']}: after OCR tier={tier} len={len(text)}")

        conn.execute(
            "UPDATE eaaid_reports SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (text, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        print(f"[eaaid parse] {row['case_id']}: tier={tier} len={len(text)}", flush=True)
        parsed_count += 1

    return parsed_count


def _classify_tier(text):
    if len(text) >= MIN_NARRATIVE:
        return "pdf"
    if len(text) >= SCANNED_FLOOR:
        return "short"
    if text:
        return "scanned"
    return "none"


def build(conn):
    """Emit eaaid_accidents rows from parsed reports.

    Rows with narrative < MIN_NARRATIVE are marked 'skipped'.
    Rows with superseded_by set are excluded from eaaid_accidents so that
    only the final (superseding) report produces an accident page.
    After building, purge any stale accident rows whose eaaid_reports row
    now has superseded_by set (handles re-runs after a final report arrives).
    """
    rows = conn.execute(
        "SELECT case_id, report_type, category, event_date, location, aircraft, "
        "registration, narrative_text, source_url, superseded_by "
        "FROM eaaid_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        # Skip rows that are superseded by a later final report
        if row["superseded_by"]:
            print(f"[eaaid build] {row['case_id']}: skipped (superseded by {row['superseded_by']})", flush=True)
            continue

        narrative = row["narrative_text"] or ""
        if len(narrative) < MIN_NARRATIVE:
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, f"short-narrative-{len(narrative)}", db.now_ms(), row["case_id"]),
            )
            conn.commit()
            print(f"[eaaid build] {row['case_id']}: skipped (narrative {len(narrative)} chars)", flush=True)
            continue

        # category from listing: 'Accident' or 'Incident'
        cat_raw = (row["category"] or "").strip()

        conn.execute(
            "INSERT OR REPLACE INTO eaaid_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, "
            "fatalities_total, phase, category, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["event_date"],
                row["aircraft"],
                row["registration"],
                None,              # operator not in listing; extracted from PDF by build-source-narratives
                row["location"],
                "EG",
                narrative,
                None,              # probable_cause extracted by build-source-narratives
                row["source_url"],
                row["report_type"],
                _make_site_slug(row["aircraft"], row["registration"], row["location"]),
                None,              # fatalities_total: NULL (v2 col, OK)
                None,              # phase: NULL
                cat_raw or None,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE eaaid_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
        print(f"[eaaid build] {row['case_id']}: built ({len(narrative)} chars)", flush=True)

    # Purge stale accident rows for any superseded reports (handles re-runs
    # after a final report is added and interims are marked superseded_by).
    superseded = conn.execute(
        "SELECT case_id FROM eaaid_reports WHERE superseded_by IS NOT NULL"
    ).fetchall()
    purged = 0
    for sup_row in superseded:
        deleted = conn.execute(
            "DELETE FROM eaaid_accidents WHERE case_id=?", (sup_row["case_id"],)
        ).rowcount
        if deleted:
            purged += 1
            print(f"[eaaid build] purged superseded accident page: {sup_row['case_id']}", flush=True)
    if purged:
        conn.commit()

    return built


def _make_site_slug(aircraft, registration, location):
    """Build a URL-safe slug for the accident page."""
    parts = []
    for s in (aircraft, registration, location):
        if s:
            parts.append(re.sub(r"[^A-Za-z0-9]+", "-", s.strip()).strip("-").lower())
    slug = "-".join(p for p in parts if p)
    return slug[:120] if slug else "unknown"
