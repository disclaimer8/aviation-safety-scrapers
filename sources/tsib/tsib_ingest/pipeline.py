# tsib_ingest/pipeline.py
"""
discover -> fetch(+parse) -> build pipeline for TSIB Singapore.

discover() walks listing pages page=1..max with CLAMP-STOP (the site's
`?page=N` is a no-op that always returns the same content, so we stop as
soon as a page's first PDF URL repeats one already seen).  Every page
already carries the FULL ~100-item inline-JSON catalogue, so one fetch is
normally enough.  Rows are keyed on pdf_url; case_id is NULL until fetch.

fetch() downloads each new row's PDF, pdftotext (tier 'pdf'); extracts the
formal case_id from the text (UUID-path fallback + collision suffix).  A
PDF with no usable text layer is tier 'scanned' (kept for later).

build() promotes 'parsed' rows with narrative >= floor into tsib_accidents
(country SG, source_url = the entry's PDF URL).
"""
import os
import sys
import time

from . import db, pdf, tsib
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False, max_pages=None):
    """Walk listing pages (clamp-stop); INSERT new rows. Returns inserted."""
    cap = max_pages if max_pages is not None else tsib.MAX_PAGES
    inserted = 0
    seen_first = set()
    page = 1
    while page <= cap:
        time.sleep(tsib.DELAY)
        try:
            page_html = tsib.fetch_listing_page(client, page)
        except Exception as e:
            if page > 1:
                break
            print(f"[tsib discover] page {page}: failed: {e}", file=sys.stderr)
            break

        items = tsib.parse_listing(page_html)
        if not items:
            break

        first = items[0]["pdf_url"]
        if first in seen_first:
            break  # clamp: this page repeats a page we already walked
        seen_first.add(first)

        for it in items:
            if conn.execute(
                "SELECT 1 FROM tsib_reports WHERE pdf_url=?",
                (it["pdf_url"],),
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO tsib_reports "
                "(pdf_url, case_id, page_url, title, synopsis, report_kind, "
                "aircraft, registration, date_of_occurrence, location, "
                "status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    it["pdf_url"],
                    None,  # case_id resolved at fetch (from the PDF text)
                    it["page_url"],
                    it["title"],
                    None,  # no synopsis on the listing; narrative is the PDF
                    it["report_kind"],
                    it["aircraft"],
                    it["registration"],
                    it["event_date"],
                    None,  # location not on the listing
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
    """Download PDF, pdftotext, resolve case_id; tier pdf/scanned."""
    rows = conn.execute(
        "SELECT pdf_url FROM tsib_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM tsib_reports WHERE case_id IS NOT NULL"
        )
    }
    n = 0
    for row in rows:
        pdf_url = row["pdf_url"]
        n += 1
        time.sleep(tsib.DELAY)
        text = ""
        tier = "scanned"
        pdf_path = None
        try:
            uuid = tsib.uuid_from_url(pdf_url) or str(abs(hash(pdf_url)))[:10]
            pdf_path = os.path.join(pdf_dir, f"{uuid}.pdf")
            tsib.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
            if len(text) >= _NARRATIVE_FLOOR:
                tier = "pdf"
        except Exception as e:
            print(f"[tsib fetch] {pdf_url}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new' (transient download failure → retry)

        case_id = tsib.make_case_id(text, pdf_url, taken=taken)
        taken.add(case_id)
        try:
            conn.execute(
                "UPDATE tsib_reports SET case_id=?, narrative_text=?, "
                "source_tier=?, pdf_path=?, status=?, updated_at=? "
                "WHERE pdf_url=?",
                (case_id, text, tier, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), pdf_url),
            )
            conn.commit()
        except Exception as e:
            print(f"[tsib fetch] {pdf_url}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into tsib_accidents."""
    rows = conn.execute(
        "SELECT pdf_url, case_id, page_url, report_kind, aircraft, "
        "registration, location, date_of_occurrence, narrative_text "
        "FROM tsib_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE tsib_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO tsib_accidents "
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
                "SG",
                narrative,
                None,
                row["pdf_url"],
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE tsib_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
