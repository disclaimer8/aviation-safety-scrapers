# aaiu_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for AAIU Ireland.

discover() pages the open WP REST API (per_page=100, stop on empty page),
parses title metadata, stores the synopsis, inserts rows keyed on the WP
post id (case_id = AAIU report number YYYY-NNN, else wp-{id}).

fetch() GETs the report PAGE, finds the uploads PDF href, downloads +
pdftotext (tier 'pdf').  Posts without a PDF — or whose PDF has no text
layer — fall back to the REST synopsis (tier 'html' / 'scanned').

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
aaiu_accidents (country IE).
"""
import os
import sys
import time

from . import aaiu, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False, max_pages=None):
    """Page the REST API; INSERT new rows. Returns inserted count."""
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM aaiu_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break
        time.sleep(aaiu.DELAY)
        try:
            rows = aaiu.fetch_listing_page(client, page)
        except Exception as e:
            # WP returns 400 rest_post_invalid_page_number past the end
            if page > 1:
                break
            print(f"[aaiu discover] page {page}: failed: {e}", file=sys.stderr)
            break
        if not rows:
            break
        for r in rows:
            wp_id = r["id"]
            if conn.execute(
                "SELECT 1 FROM aaiu_reports WHERE wp_id=?", (wp_id,)
            ).fetchone():
                continue
            title = r["title"]["rendered"]
            meta = aaiu.parse_title(title)
            case_id = aaiu.make_case_id(meta["case_id"], wp_id, taken=taken)
            taken.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaiu_reports "
                "(wp_id, case_id, page_url, title, synopsis, report_kind, "
                "aircraft, registration, date_of_occurrence, location, "
                "status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    wp_id,
                    case_id,
                    r["link"],
                    aaiu._strip(title),
                    aaiu.synopsis_text(r["content"]["rendered"]),
                    meta["report_kind"],
                    meta["aircraft"],
                    meta["registration"],
                    meta["event_date"],
                    meta["location"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
        page += 1
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: GET the report page, download its PDF,
    pdftotext; fall back to the synopsis when no usable PDF.
    """
    rows = conn.execute(
        "SELECT wp_id, case_id, page_url, synopsis FROM aaiu_reports "
        "WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(aaiu.DELAY)
        try:
            page_html = aaiu.fetch_page(client, row["page_url"])
        except Exception as e:
            print(f"[aaiu fetch] {case_id}: page failed: {e}", file=sys.stderr)
            continue  # stays 'new'

        pdf_url = aaiu.find_pdf_url(page_html)
        text = ""
        tier = "html"
        pdf_path = None
        if pdf_url:
            pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
            try:
                time.sleep(aaiu.DELAY)
                aaiu.download_pdf(client, pdf_url, pdf_path)
                text = pdf.extract_text(pdf_path)
                tier = "pdf"
            except Exception as e:
                print(f"[aaiu fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
        if len(text) < _NARRATIVE_FLOOR:
            # no PDF / scan → synopsis fallback
            syn = row["synopsis"] or ""
            if len(syn) >= len(text):
                text = syn
                tier = "scanned" if pdf_url else "html"

        try:
            conn.execute(
                "UPDATE aaiu_reports SET narrative_text=?, source_tier=?, "
                "pdf_url=?, pdf_path=?, status=?, updated_at=? WHERE wp_id=?",
                (text, tier, pdf_url, pdf_path, db.STATUS_PARSED, db.now_ms(),
                 row["wp_id"]),
            )
            conn.commit()
        except Exception as e:
            print(f"[aaiu fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into aaiu_accidents."""
    rows = conn.execute(
        "SELECT wp_id, case_id, page_url, report_kind, aircraft, registration, "
        "location, date_of_occurrence, narrative_text "
        "FROM aaiu_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaiu_reports SET status=?, updated_at=? WHERE wp_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["wp_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO aaiu_accidents "
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
                "IE",
                narrative,
                None,
                row["page_url"] or "https://aaiu.ie/",
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiu_reports SET status=?, updated_at=? WHERE wp_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["wp_id"]),
        )
        conn.commit()
        built += 1
    return built
