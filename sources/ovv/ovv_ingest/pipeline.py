# ovv_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for OVV / Dutch Safety Board.

discover() walks `?_aviation_tax=uncategorized&_page=1..` (stop-on-empty),
inserting investigation slugs as case_ids.

fetch() GETs the detail page, ranks the hash-slug `-pdf/` doc links
(EN main report first), downloads candidates in order until one yields
pdftotext text >= the floor (scans/letters fall through), else keeps the
page summary (tier 'html').  Doc-less (ongoing) rows keep metadata and
stay 'new' — self-heal on the weekly cycle.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
ovv_accidents (country NL).
"""
import os
import sys
import time

from . import db, ovv, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300
_PDF_TEXT_FLOOR = 2000  # below this a doc is a scan/letter → try next doc
_MAX_DOC_TRIES = 3


def discover(conn, client, full=False, max_pages=None):
    """Walk listing pages; INSERT new rows. Returns inserted count."""
    inserted = 0
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break
        time.sleep(ovv.DELAY)
        try:
            html = ovv.fetch_listing_page(client, page)
        except Exception as e:
            print(f"[ovv discover] page {page}: failed: {e}", file=sys.stderr)
            break
        rows = ovv.parse_listing(html)
        if not rows:
            break
        for r in rows:
            if conn.execute(
                "SELECT 1 FROM ovv_reports WHERE case_id=?", (r["slug"],)
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO ovv_reports (case_id, detail_url, status, "
                "discovered_at, updated_at) VALUES (?,?,?,?,?)",
                (r["slug"], r["url"], db.STATUS_NEW, ts, ts),
            )
            inserted += 1
        conn.commit()
        page += 1
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: detail page → ranked docs → first with a
    real text layer wins; summary fallback; doc-less rows stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, detail_url FROM ovv_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(ovv.DELAY)
        try:
            html = ovv.fetch_page(client, row["detail_url"])
            d = ovv.parse_detail(html)
        except Exception as e:
            print(f"[ovv fetch] {case_id}: page failed: {e}", file=sys.stderr)
            continue

        base_meta = (d["title"], d["summary"], d["registration"],
                     d["event_date"])

        if not d["doc_urls"]:
            # ongoing / doc-less — keep metadata, stay 'new' (self-heal)
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, updated_at=? WHERE case_id=?",
                (*base_meta, db.now_ms(), case_id),
            )
            conn.commit()
            continue

        text, used_url, lang = "", None, None
        pdf_path = os.path.join(pdf_dir, f"{case_id[:60]}.pdf")
        for doc_url in d["doc_urls"][:_MAX_DOC_TRIES]:
            time.sleep(ovv.DELAY)
            try:
                ovv.download_pdf(client, doc_url, pdf_path)
                t = pdf.extract_text(pdf_path)
            except Exception as e:
                print(f"[ovv fetch] {case_id}: doc failed: {e}", file=sys.stderr)
                continue
            if len(t) >= _PDF_TEXT_FLOOR:
                text, used_url, lang = t, doc_url, ovv.doc_lang(doc_url)
                break
            if len(t) > len(text):
                text, used_url, lang = t, doc_url, ovv.doc_lang(doc_url)

        tier = "pdf"
        if len(text) < _NARRATIVE_FLOOR:
            summary = d["summary"] or ""
            if len(summary) > len(text):
                text, tier, lang = summary, "html", "en"
            else:
                tier = "scanned"

        try:
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, lang=?, narrative_text=?, "
                "source_tier=?, pdf_url=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (*base_meta, lang, text, tier, used_url,
                 pdf_path if used_url else None,
                 db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[ovv fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into ovv_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, title, registration, "
        "date_of_occurrence, narrative_text FROM ovv_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ovv_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(None, row["registration"], row["title"])
        conn.execute(
            "INSERT OR REPLACE INTO ovv_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                None,
                row["registration"],
                None,
                row["title"],
                "NL",
                narrative,
                None,
                row["detail_url"] or "https://onderzoeksraad.nl/",
                None,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ovv_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
