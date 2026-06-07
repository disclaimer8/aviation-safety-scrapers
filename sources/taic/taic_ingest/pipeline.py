# taic_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for TAIC (New Zealand).

discover() walks the paginated listing (ALL modes — the server ignores the
aviation filter), keeps AO-* cards only, inserts new case_ids with listing
metadata, and stops after the first page with zero cards.  For EXISTING rows
it watches the pill: when an "In progress" inquiry flips to "Published" the
row is reset to status='new' so the next fetch picks up the final report
(self-healing — open inquiries close over time).

fetch() processes status='new' rows whose pill is 'Published' (in-progress
pages carry no report → leave them at 'new'; they cost nothing).  Narrative
strategy per row:
    1. GET the inquiry page; parse metadata + rich-content narrative.
    2. HTML narrative >= _HTML_FLOOR → source_tier='html', done.
    3. Else, if a site-local PDF exists: download → pdftotext.
       Text >= _HTML_FLOOR → source_tier='pdf'.
       Else → source_tier='scanned' (pre-~2000 photocopier scans), keep the
       short HTML text if any.
    4. → status 'parsed' either way; build() applies the final floor.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
taic_accidents; shorter rows → 'skipped'.
"""
import os
import sys
import time

from . import db, pdf, taic
from .text import make_site_slug

_NARRATIVE_FLOOR = 300   # final build floor (prod renders noindex below 600)
_HTML_FLOOR = 1500       # below this the HTML page is a stub → try the PDF
_MAX_EMPTY_PAGES = 1     # listing stop signal


def discover(conn, client, full=False, max_pages=None):
    """
    Walk listing pages 0.. until an empty page; INSERT new AO-* rows and
    reset previously-known rows whose pill flipped to Published.

    Returns: number of rows inserted.
    """
    inserted = 0
    page = 0
    empty = 0
    while True:
        if max_pages is not None and page >= max_pages:
            break
        time.sleep(taic.DELAY)
        try:
            html = taic.fetch_listing_page(client, page)
        except Exception as e:
            print(f"[taic discover] page {page}: failed: {e}", file=sys.stderr)
            break
        cards = taic.parse_listing(html)
        if not cards:
            empty += 1
            if empty >= _MAX_EMPTY_PAGES:
                break
            page += 1
            continue
        empty = 0
        for c in cards:
            if not taic.is_aviation(c["case_id"]):
                continue
            row = conn.execute(
                "SELECT status, pill FROM taic_reports WHERE case_id=?",
                (c["case_id"],),
            ).fetchone()
            ts = db.now_ms()
            if row:
                # Self-heal: in-progress inquiry got published → re-fetch
                if row["pill"] != c["pill"] and c["pill"] == "Published":
                    conn.execute(
                        "UPDATE taic_reports SET pill=?, publish_date=?, "
                        "status=?, updated_at=? WHERE case_id=?",
                        (c["pill"], c["publish_date"], db.STATUS_NEW, ts,
                         c["case_id"]),
                    )
                continue
            conn.execute(
                "INSERT INTO taic_reports "
                "(case_id, inquiry_url, title, summary, date_of_occurrence, "
                "publish_date, pill, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    c["case_id"],
                    c["inquiry_url"],
                    c["title"],
                    c["summary"],
                    c["event_date"],
                    c["publish_date"],
                    c["pill"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
        page += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' AND pill='Published' row: GET the inquiry page,
    parse narrative + metadata, fall back to PDF when the HTML is a stub,
    advance to 'parsed'.

    Per-row try/except: a failing row stays 'new' for retry next cycle.

    Returns: number of rows iterated.
    """
    rows = conn.execute(
        "SELECT case_id, inquiry_url FROM taic_reports "
        "WHERE status=? AND pill='Published'",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(taic.DELAY)
        try:
            html = taic.fetch_page(client, row["inquiry_url"])
            parsed = taic.parse_inquiry(html)
        except Exception as e:
            print(f"[taic fetch] {case_id}: failed: {e}", file=sys.stderr)
            continue  # stays 'new' for retry

        narrative = parsed["narrative_text"] or ""
        tier = "html"
        pdf_url = parsed["pdf_urls"][0] if parsed["pdf_urls"] else None
        pdf_path = None

        if len(narrative) < _HTML_FLOOR and pdf_url:
            pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
            try:
                time.sleep(taic.DELAY)
                taic.download_pdf(client, pdf_url, pdf_path)
                pdf_text = pdf.extract_text(pdf_path)
            except Exception as e:
                print(f"[taic fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
                pdf_text = ""
            if len(pdf_text) >= _HTML_FLOOR:
                narrative = pdf_text
                tier = "pdf"
            else:
                # photocopier scan (no text layer) — keep short HTML if any
                tier = "scanned"

        try:
            conn.execute(
                "UPDATE taic_reports SET narrative_text=?, source_tier=?, "
                "registration=COALESCE(?, registration), "
                "aircraft=COALESCE(?, aircraft), "
                "operator=COALESCE(?, operator), "
                "location=COALESCE(?, location), "
                "injuries=COALESCE(?, injuries), "
                "date_of_occurrence=COALESCE(?, date_of_occurrence), "
                "pdf_url=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (
                    narrative,
                    tier,
                    parsed["registration"],
                    parsed["aircraft"],
                    parsed["operator"],
                    parsed["location"],
                    parsed["injuries"],
                    parsed["event_date"],
                    pdf_url,
                    pdf_path,
                    db.STATUS_PARSED,
                    db.now_ms(),
                    case_id,
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"[taic fetch] {case_id}: db update failed: {e}", file=sys.stderr)
    return len(rows)


def build(conn):
    """
    Promote 'parsed' rows with a substantive narrative into taic_accidents.
    Rows under _NARRATIVE_FLOOR (incl. scans) → 'skipped'.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, inquiry_url, title, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, source_tier "
        "FROM taic_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE taic_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO taic_accidents "
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
                "NZ",
                narrative,
                None,
                row["inquiry_url"] or "",
                row["title"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE taic_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
