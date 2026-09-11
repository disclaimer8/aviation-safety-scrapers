# eaaid_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for EAAID (Egypt).

Fetching strategy:
  Straight HTTP through httpc, like every other package.

  This used to shell out to `ssh HETZNER curl ...` for both the listing and
  every PDF, then rsync the files back, because the site was recorded as
  timing out from the Mac and the mini-PC. That is no longer true: checked
  2026-09-11, the listing returns HTTP 200 in about a second (three of three
  attempts, one slow at 31s but successful) and carries the <tbody> the
  parser needs, and a report PDF downloads directly with no session cookie at
  all — 296 KB, correct %PDF magic.

  Removing the shell removes a category, not just a bug: the href captured
  from the listing was interpolated into a remote shell command, which made a
  quote in someone else's HTML into command execution on the fetch host. That
  was hardened in place first; this deletes the surface instead. It also ends
  this scraper's dependency on a second machine, and brings it under the retry
  policy, the SSRF guard and the robots gate that httpc carries.

Stages:
  discover  — fetch listing HTML, parse, insert new rows
  fetch     — download PDFs straight to pdf_dir
  parse     — extract text, strip Arabic, classify tier
  build     — emit eaaid_accidents rows
  all       — discover + fetch + parse + build
"""
import os
import re
import shlex
import sys
import time

from . import db, eaaid
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

FETCH_DELAY  = 2.0   # seconds between individual PDF downloads on hetzner





def discover(conn, client):
    """Fetch the listing, parse the table, insert new rows.

    Returns (inserted, total) counts.
    """
    resp = client.get(eaaid.LISTING_URL, headers={"Referer": eaaid.LISTING_URL})
    resp.raise_for_status()
    html = resp.text
    if "<tbody>" not in html:
        # Fail loudly. A listing that parses to nothing is indistinguishable
        # from a site with no reports, and this source has 87.
        raise RuntimeError(
            "[eaaid discover] listing did not return the expected table; "
            f"got HTTP {resp.status_code}, {len(html)} chars, snippet: "
            f"{html[:200]!r}"
        )

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


def fetch(conn, client, pdf_dir):
    """Download PDFs for all status='new' rows straight to pdf_dir.

    Each report URL is refused unless eaaid.valid_source_url accepts it. The
    check outlived the remote shell it was written for: rows inserted before
    the href allowlist landed can hold anything, and a URL we cannot vouch for
    should not be fetched even when nothing will interpret it as a command.
    """
    os.makedirs(pdf_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT case_id, guid, source_url FROM eaaid_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    if not rows:
        print("[eaaid fetch] no new rows to fetch")
        return 0

    print(f"[eaaid fetch] {len(rows)} PDFs to download")
    ok_count = fail_count = 0

    for row in rows:
        case_id = row["case_id"]
        url     = row["source_url"]
        dest    = os.path.join(pdf_dir, eaaid.pdf_filename(case_id, row["guid"]))

        if not eaaid.valid_source_url(url):
            print(f"[eaaid fetch] {case_id}: refusing malformed source_url "
                  f"{url[:120]!r}", file=sys.stderr)
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, "bad-source-url", db.now_ms(), case_id),
            )
            conn.commit()
            fail_count += 1
            continue

        if os.path.exists(dest) and os.path.getsize(dest) > 500:
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    print(f"[eaaid fetch] {case_id}: already on disk")
                    conn.execute(
                        "UPDATE eaaid_reports SET pdf_path=?, status=?, updated_at=? "
                        "WHERE case_id=?",
                        (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
                    )
                    conn.commit()
                    ok_count += 1
                    continue

        print(f"[eaaid fetch] {case_id}: downloading...", flush=True)
        try:
            resp = client.get(url, headers={"Referer": eaaid.LISTING_URL})
            resp.raise_for_status()
            content = resp.content
        except Exception as exc:
            fail_count += 1
            print(f"[eaaid fetch]   {case_id}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, "fetch-failed", db.now_ms(), case_id),
            )
            conn.commit()
            continue
        time.sleep(FETCH_DELAY)

        if content[:4] != b"%PDF":
            fail_count += 1
            print(f"[eaaid fetch]   {case_id}: not a PDF "
                  f"(magic={content[:4]!r}), skipping")
            conn.execute(
                "UPDATE eaaid_reports SET status=?, skip_reason=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, "not-pdf", db.now_ms(), case_id),
            )
            conn.commit()
            continue

        with open(dest, "wb") as f:
            f.write(content)
        print(f"[eaaid fetch]   {case_id}: OK ({len(content)} bytes)")
        conn.execute(
            "UPDATE eaaid_reports SET pdf_path=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()
        ok_count += 1

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
