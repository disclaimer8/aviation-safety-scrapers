# nsia_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for NSIA Norway.

discover() walks listing pages 0.. until an empty page; inserts new rows
with listing metadata (incl. the per-row Lang. — Norwegian rows get their
NO→EN rewrite at Phase 3, the BFU/BEA precedent).

fetch() GETs the detail page (operator + occurrence type), downloads the
constructable PDF, pdftotext.  Empty text (1950s-70s scans) → 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
nsia_accidents (country NO).
"""
import os
import sys
import time

from . import db, nsia, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300
_MAX_EMPTY_PAGES = 1


def discover(conn, client, full=False, max_pages=None):
    """Walk listing pages; INSERT new rows. Returns inserted count."""
    inserted = 0
    page = 1  # ⚠️ 1-indexed: ?page=0 silently serves the same content as ?page=1
    empty = 0
    while True:
        if max_pages is not None and page > max_pages:
            break
        time.sleep(nsia.DELAY)
        try:
            html = nsia.fetch_listing_page(client, page)
        except Exception as e:
            print(f"[nsia discover] page {page}: failed: {e}", file=sys.stderr)
            break
        rows = nsia.parse_listing(html)
        if not rows:
            empty += 1
            if empty >= _MAX_EMPTY_PAGES:
                break
            page += 1
            continue
        empty = 0
        for r in rows:
            if conn.execute(
                "SELECT 1 FROM nsia_reports WHERE case_id=?", (r["case_id"],)
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO nsia_reports "
                "(case_id, detail_url, lang, aircraft, registration, "
                "date_of_occurrence, location, status, discovered_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    r["case_id"],
                    r["detail_url"],
                    r["lang"],
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
        page += 1
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: GET the detail page (operator/type/title),
    download the PDF, pdftotext, advance to 'parsed'.  Failing rows stay
    'new' for retry.
    """
    rows = conn.execute(
        "SELECT case_id, detail_url FROM nsia_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(nsia.DELAY)
        try:
            detail_html = nsia.fetch_page(client, row["detail_url"])
            detail = nsia.parse_detail(detail_html)
        except Exception as e:
            print(f"[nsia fetch] {case_id}: detail failed: {e}", file=sys.stderr)
            continue

        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        url = nsia.pdf_url(row["detail_url"])
        try:
            time.sleep(nsia.DELAY)
            nsia.download_pdf(client, url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[nsia fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue

        tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE nsia_reports SET narrative_text=?, source_tier=?, "
                "title=?, operator=?, report_kind=?, pdf_url=?, pdf_path=?, "
                "status=?, updated_at=? WHERE case_id=?",
                (
                    text,
                    tier,
                    detail["title"],
                    detail["operator"],
                    detail["report_kind"],
                    url,
                    pdf_path,
                    db.STATUS_PARSED,
                    db.now_ms(),
                    case_id,
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"[nsia fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into nsia_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, report_kind, aircraft, registration, "
        "operator, location, date_of_occurrence, narrative_text "
        "FROM nsia_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE nsia_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO nsia_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "NO",
                narrative,
                None,
                row["detail_url"] or "https://nsia.no/",
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE nsia_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
