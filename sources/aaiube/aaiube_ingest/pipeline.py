# aaiube_ingest/pipeline.py
"""
discover → fetch → build pipeline for AAIU Belgium.

discover() GETs the single listing page, parses every PDF-bearing table row,
derives a case_id from the PDF filename, and INSERTs rows keyed on pdf_url
(the natural primary key — one report PDF per row). Idempotent: existing
pdf_urls are skipped.

fetch() downloads each new PDF, runs pdftotext (tier 'pdf'); PDFs with no
text layer stay below the floor and are marked 'scanned'.

build() promotes 'parsed' rows whose narrative >= _NARRATIVE_FLOOR into
aaiube_accidents (country BE).
"""
import os
import sys
import time

from . import aaiube, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False, max_pages=None):
    """GET the listing page, parse rows, INSERT new ones. Returns count."""
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM aaiube_reports WHERE case_id IS NOT NULL"
        )
    }
    try:
        page_html = aaiube.fetch_listing(client)
    except Exception as e:
        print(f"[aaiube discover] listing failed: {e}", file=sys.stderr)
        return 0

    rows = aaiube.parse_listing(page_html)
    inserted = 0
    for r in rows:
        if conn.execute(
            "SELECT 1 FROM aaiube_reports WHERE pdf_url=?", (r["pdf_url"],)
        ).fetchone():
            continue
        case_id = aaiube.derive_case_id(r["pdf_url"], year=r["year"], taken=taken)
        taken.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaiube_reports "
            "(pdf_url, case_id, date_of_occurrence, aircraft, casualties, "
            "location, status, report_kind, lang, proc_status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["pdf_url"],
                case_id,
                r["date_of_occurrence"],
                r["aircraft"],
                r["casualties"],
                r["location"],
                r["status"],
                r["report_kind"],
                r["lang"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each 'new' row: download the PDF and pdftotext it."""
    rows = conn.execute(
        "SELECT pdf_url, case_id FROM aaiube_reports WHERE proc_status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        time.sleep(aaiube.DELAY)
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        text = ""
        tier = "scanned"
        try:
            aaiube.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
            tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        except Exception as e:
            print(f"[aaiube fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            pdf_path = None  # stays new on next run? mark parsed to avoid loop
        try:
            conn.execute(
                "UPDATE aaiube_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, proc_status=?, updated_at=? WHERE pdf_url=?",
                (text, tier, pdf_path, db.STATUS_PARSED, db.now_ms(), pdf_url),
            )
            conn.commit()
        except Exception as e:
            print(f"[aaiube fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into aaiube_accidents."""
    rows = conn.execute(
        "SELECT pdf_url, case_id, date_of_occurrence, aircraft, location, "
        "report_kind, narrative_text FROM aaiube_reports WHERE proc_status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaiube_reports SET proc_status=?, updated_at=? "
                "WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["location"], row["case_id"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO aaiube_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                None,
                None,
                row["location"],
                "BE",
                narrative,
                None,
                row["pdf_url"] or aaiube.LISTING_URL,
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiube_reports SET proc_status=?, updated_at=? "
            "WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
