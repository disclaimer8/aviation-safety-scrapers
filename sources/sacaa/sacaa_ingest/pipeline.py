# sacaa_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for SACAA AIID (South Africa).

discover() GETs the two listing pages (main + archive), parses every table
row with a blob-PDF href, inserts new rows keyed on pdf_url with a case_id
(numeric AIID id when present, else registration+date slug).

fetch() downloads each PDF and runs pdftotext.  Metadata comes from the
LISTING (complete there) — no PDF metadata parse.  Empty pdftotext output
(scanned archive-era files) → source_tier='scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
sacaa_accidents (country ZA).
"""
import os
import sys
import time

from . import db, pdf, sacaa
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """
    Parse both listing pages; INSERT new (pdf_url-keyed) rows.
    Returns: number of rows inserted.
    """
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM sacaa_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0
    for url in (sacaa.MAIN_URL, sacaa.ARCHIVE_URL):
        time.sleep(sacaa.DELAY)
        try:
            html = sacaa.fetch_page(client, url)
        except Exception as e:
            print(f"[sacaa discover] {url}: failed: {e}", file=sys.stderr)
            continue
        for r in sacaa.parse_listing(html):
            if conn.execute(
                "SELECT 1 FROM sacaa_reports WHERE pdf_url=?", (r["pdf_url"],)
            ).fetchone():
                continue
            case_id = sacaa.make_case_id(
                r["name"], r["registration"], r["event_date"], taken=taken
            )
            taken.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO sacaa_reports "
                "(pdf_url, case_id, report_kind, aircraft, registration, "
                "date_of_occurrence, location, status, discovered_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    r["pdf_url"],
                    case_id,
                    r["report_kind"],
                    r["aircraft"],
                    r["registration"],
                    r["event_date"],
                    r["location"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: download the PDF, pdftotext, advance to
    'parsed'.  Per-row try/except — a failing row stays 'new'.
    Returns: number of rows iterated.
    """
    rows = conn.execute(
        "SELECT pdf_url, case_id FROM sacaa_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(sacaa.DELAY)
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        try:
            sacaa.download_pdf(client, row["pdf_url"], pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[sacaa fetch] {case_id}: failed: {e}", file=sys.stderr)
            continue

        tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE sacaa_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, status=?, updated_at=? WHERE pdf_url=?",
                (text, tier, pdf_path, db.STATUS_PARSED, db.now_ms(),
                 row["pdf_url"]),
            )
            conn.commit()
        except Exception as e:
            print(f"[sacaa fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """
    Promote 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
    sacaa_accidents; shorter (incl. scans) → 'skipped'.
    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT pdf_url, case_id, report_kind, aircraft, registration, "
        "location, date_of_occurrence, narrative_text "
        "FROM sacaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE sacaa_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO sacaa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "ZA",
                narrative,
                None,
                row["pdf_url"],
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE sacaa_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
