# dgaccl_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for DGAC Chile.

discover() GETs each of the 7 HARDCODED per-year pages (⚠️ 2023 is the
singular 'informe-2023' slug), parses the server-rendered table into case
rows, picks the PREFERRED staged PDF per case (Final > latest Preliminar),
and INSERTs new rows keyed on case_id ('{caseNumber}-{YY}', collision-
suffixed). Rows without a usable report PDF are skipped at insert time.

fetch() downloads each new PDF + pdftotext (tier 'pdf'), extracts the CC-
registration best-effort from the text; a PDF with no usable text layer is
tiered 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
dgaccl_accidents (country CL).
"""
import os
import sys
import time

from . import dgaccl, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """Walk the 7 hardcoded year pages; INSERT new rows. Returns count."""
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM dgaccl_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0
    for year_url in dgaccl.YEAR_PAGES:
        time.sleep(dgaccl.DELAY)
        try:
            year_html = dgaccl.fetch_page(client, year_url)
        except Exception as e:
            print(f"[dgaccl discover] {year_url}: failed: {e}", file=sys.stderr)
            continue

        for rec in dgaccl.parse_year_page(year_html, year_url):
            pdf_url = rec["pdf_url"]
            if not pdf_url:
                continue  # no report PDF for this case → nothing to ingest
            if conn.execute(
                "SELECT 1 FROM dgaccl_reports WHERE pdf_url=?", (pdf_url,)
            ).fetchone():
                continue
            case_id = dgaccl.make_case_id(
                rec["case_number"], rec["yy"], taken=taken
            )
            taken.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO dgaccl_reports "
                "(case_id, pdf_url, page_url, year, case_number, title, "
                "report_kind, aircraft, registration, date_of_occurrence, "
                "location, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    pdf_url,
                    year_url,
                    rec["year"],
                    rec["case_number"],
                    rec["filename"],
                    rec["report_kind"],
                    rec["aircraft"],
                    None,  # registration filled at fetch from PDF text
                    rec["event_date"],
                    rec["location"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each status='new' row: download the PDF + pdftotext + reg extract."""
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM dgaccl_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(dgaccl.DELAY)
        try:
            dgaccl.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[dgaccl fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new'
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"  # no usable text layer

        registration = dgaccl.extract_registration(text)
        try:
            conn.execute(
                "UPDATE dgaccl_reports SET narrative_text=?, source_tier=?, "
                "registration=?, pdf_path=?, status=?, updated_at=? "
                "WHERE pdf_url=?",
                (text, tier, registration, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), pdf_url),
            )
            conn.commit()
        except Exception as e:
            print(f"[dgaccl fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into dgaccl_accidents."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, page_url, report_kind, aircraft, "
        "registration, location, date_of_occurrence, narrative_text "
        "FROM dgaccl_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE dgaccl_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        report_type = row["report_kind"]
        conn.execute(
            "INSERT OR REPLACE INTO dgaccl_accidents "
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
                "CL",
                narrative,
                None,
                row["page_url"] or dgaccl.BASE,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE dgaccl_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
