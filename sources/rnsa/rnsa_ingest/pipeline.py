# rnsa_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for RNSA Iceland (aviation).

discover() GETs each per-year archive page
(/flug/slysa-og-atvikaskyrslur/{YEAR}/) from FIRST_YEAR up to current-year+1,
tolerating 404s for unpublished/future years. Each page's `<div class="item">`
report blocks are parsed; best-effort metadata (registration, ICAO, date,
report kind, language) is read from the rich FILENAME + h3 title. New rows are
INSERTed keyed on case_id = the numeric /media/{id} (stable & unique).

fetch() downloads each new PDF + pdftotext (tier 'pdf'); a PDF with no usable
text layer is tiered 'scanned'. Registration is re-confirmed best-effort from
the PDF text when the filename carried none, and language is re-detected via a
stopword sniff of the actual text.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
rnsa_accidents (country 'IS'); event_date falls back to '{year}-01-01' when the
filename carried no parseable date.
"""
import os
import sys
import time

from . import rnsa, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False, first_year=None, last_year=None):
    """Walk the per-year archive pages; INSERT new rows. Returns count."""
    pages = rnsa.year_pages(
        first_year=first_year or rnsa.FIRST_YEAR, last_year=last_year
    )
    inserted = 0
    for year_url in pages:
        time.sleep(rnsa.DELAY)
        try:
            year_html = rnsa.fetch_page(client, year_url)
        except Exception as e:
            # 404 = unpublished/future year; tolerated and logged.
            print(f"[rnsa discover] {year_url}: skipped ({e})", file=sys.stderr)
            continue

        for rec in rnsa.parse_year_page(year_html, year_url):
            pdf_url = rec["pdf_url"]
            if not pdf_url:
                continue
            if conn.execute(
                "SELECT 1 FROM rnsa_reports WHERE case_id=? OR pdf_url=?",
                (rec["case_id"], pdf_url),
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO rnsa_reports "
                "(case_id, pdf_url, page_url, year, slug, title, report_kind, "
                "aircraft, registration, date_of_occurrence, location, lang, "
                "summary, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec["case_id"],
                    pdf_url,
                    year_url,
                    rec["year"],
                    rec["slug"],
                    rec["title"],
                    rec["report_kind"],
                    None,  # aircraft: not in listing; null ok
                    rec["registration"],
                    rec["event_date"],
                    rec["location"],
                    rec["lang"],
                    rec["summary"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each status='new' row: download the PDF + pdftotext + re-detect."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, slug, title, registration, lang "
        "FROM rnsa_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(rnsa.DELAY)
        try:
            rnsa.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[rnsa fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new', retried next cycle
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"

        # Registration: keep filename's if present, else best-effort from text.
        registration = row["registration"] or rnsa.extract_registration(text)
        # Re-detect language using the actual PDF text layer.
        lang = rnsa.detect_lang(row["slug"], text=text, title=row["title"])
        try:
            conn.execute(
                "UPDATE rnsa_reports SET narrative_text=?, source_tier=?, "
                "registration=?, lang=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (text, tier, registration, lang, pdf_path,
                 db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[rnsa fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into rnsa_accidents."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, page_url, year, report_kind, aircraft, "
        "registration, location, date_of_occurrence, lang, narrative_text "
        "FROM rnsa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE rnsa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        event_date = row["date_of_occurrence"] or \
            rnsa.fallback_event_date(row["year"])
        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO rnsa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, probable_cause, source_url, "
            "report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                event_date,
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "IS",
                row["lang"],
                narrative,
                None,
                row["page_url"] or rnsa.BASE,
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE rnsa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
