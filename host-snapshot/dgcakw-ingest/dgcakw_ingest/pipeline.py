# dgcakw_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for Kuwait DGCA (dgcakw).

discover(): Walk the hardcoded KNOWN_REPORTS catalog and INSERT new case_ids
  into dgcakw_reports. Idempotent — existing case_ids are skipped.

fetch(): for each status='new' row, download the PDF from the best Wayback
  snapshot (the live site kas2.dgca.gov.kw times out). Advance to 'fetched'.

parse(): extract text via pdftotext, strip Arabic script. Assess tier.

build(): emit dgcakw_accidents rows. Rows with narrative < _NARRATIVE_FLOOR
  are skipped.
"""
import os
import sys
import time

from . import dgcakw, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

_NARRATIVE_FLOOR = MIN_NARRATIVE   # 300 chars as per task


def discover(conn):
    """Populate dgcakw_reports from the hardcoded catalog.

    Returns: number of rows inserted.
    """
    inserted = 0
    for report in dgcakw.KNOWN_REPORTS:
        case_id = report["case_id"]
        if conn.execute(
            "SELECT 1 FROM dgcakw_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue

        archive_url = dgcakw.wayback_pdf_url(
            report["pdf_url_live"], report["archive_ts"]
        )
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO dgcakw_reports "
            "(case_id, report_url, pdf_url, archive_ts, archive_url, title, "
            "event_class, aircraft, registration, date_of_occurrence, location, "
            "operator, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                report["pdf_url_live"],
                report["pdf_url_live"],
                report["archive_ts"],
                archive_url,
                report["title"],
                report["report_type"],
                report["aircraft"],
                report["registration"],
                report["event_date"],
                report["location"],
                report["operator"],
                "en",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download the PDF for each status='new' row using the Wayback archive URL.

    Falls back gracefully: per-row try/except, rows that fail stay at 'new'.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, archive_url, report_url FROM dgcakw_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        # Prefer Wayback URL; fall back to live if not set
        url = row["archive_url"] or row["report_url"]
        if not url:
            print(f"[dgcakw fetch] {case_id}: no URL, skipping", file=sys.stderr)
            continue

        safe_id = case_id.replace("/", "_").replace(" ", "_")
        dest = os.path.join(pdf_dir, safe_id + ".pdf")

        try:
            time.sleep(dgcakw.DELAY)
            print(f"[dgcakw fetch] {case_id}: GET {url[:80]}...", flush=True)
            resp = client.get(url)
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            print(f"[dgcakw fetch] {case_id}: saved {len(resp.content)} bytes", flush=True)
        except Exception as exc:
            print(f"[dgcakw fetch] {case_id}: download failed: {exc}", file=sys.stderr)
            continue

        try:
            conn.execute(
                "UPDATE dgcakw_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[dgcakw fetch] {case_id}: db error: {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract and clean text from each status='fetched' PDF.

    source_tier:
      'pdf'     — len >= MIN_NARRATIVE  (full report)
      'short'   — SCANNED_FLOOR <= len < MIN_NARRATIVE
      'scanned' — 0 < len < SCANNED_FLOOR (likely image PDF; OCR needed)
      'none'    — empty / no file

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM dgcakw_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif full_text:
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE dgcakw_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        print(f"[dgcakw parse] {row['case_id']}: tier={tier} len={len(full_text)}", flush=True)

    return len(rows)


def build(conn):
    """Emit dgcakw_accidents records for each buildable status='parsed' row.

    Rows with narrative shorter than _NARRATIVE_FLOOR are skipped.
    country is always 'KW' (Kuwait).

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, archive_url "
        "FROM dgcakw_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE dgcakw_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            print(f"[dgcakw build] {row['case_id']}: skipped (narrative too short: {len(narrative)})", flush=True)
            continue

        # Use live URL as source_url; archive_url as fallback
        source_url = row["archive_url"] or row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO dgcakw_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "KW",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE dgcakw_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
        print(f"[dgcakw build] {row['case_id']}: built (narrative {len(narrative)} chars)", flush=True)

    return built
