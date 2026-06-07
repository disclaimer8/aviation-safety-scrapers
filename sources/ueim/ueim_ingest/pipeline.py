# ueim_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for UEIM Turkey.

discover() GETs the canonical Turkish listing (one server-rendered page, no
pagination), parses its table rows, harvests the report PDF href per row, and
INSERTs new rows keyed on case_id (the PDF slug). It then GETs the English
listing and ADDs only PDFs not already seen on the TR page (lang='en').
Dedup is by PDF URL throughout — the same registration can belong to two
different accidents, so registration is NEVER used as a key.

fetch() downloads each new PDF + pdftotext (tier 'pdf'), best-effort verifies a
TC- registration from the text when the slug prefix didn't yield one; a PDF with
no usable text layer is tiered 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
ueim_accidents (country TR).
"""
import os
import sys
import time

from . import ueim, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def _insert_records(conn, records, taken):
    """INSERT new report rows (deduped by pdf_url + case_id). Returns count."""
    inserted = 0
    for rec in records:
        pdf_url = rec["pdf_url"]
        if not pdf_url:
            continue
        if pdf_url in taken["urls"]:
            continue
        if conn.execute(
            "SELECT 1 FROM ueim_reports WHERE pdf_url=?", (pdf_url,)
        ).fetchone():
            taken["urls"].add(pdf_url)
            continue
        case_id = rec["case_id"]
        # case_id (the slug) is globally unique in the uploads path, but guard
        # collisions defensively with a numeric suffix.
        cand = case_id
        n = 2
        while cand in taken["ids"]:
            cand = f"{case_id}-{n}"
            n += 1
        case_id = cand

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ueim_reports "
            "(case_id, pdf_url, page_url, lang, title, report_type, aircraft, "
            "registration, date_of_occurrence, location, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                pdf_url,
                rec["page_url"],
                rec["lang"],
                rec["title"],
                rec["report_type"],
                None,  # aircraft: not in listing; best-effort later
                rec["registration"],
                rec["event_date"],
                rec["location"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        taken["urls"].add(pdf_url)
        taken["ids"].add(case_id)
        inserted += 1
    return inserted


def discover(conn, client, full=False):
    """
    Parse the TR listing (canonical) then the EN listing (add-only). INSERT new
    rows. Returns total inserted count.
    """
    taken = {
        "ids": {
            r["case_id"]
            for r in conn.execute(
                "SELECT case_id FROM ueim_reports WHERE case_id IS NOT NULL"
            )
        },
        "urls": {
            r["pdf_url"]
            for r in conn.execute(
                "SELECT pdf_url FROM ueim_reports WHERE pdf_url IS NOT NULL"
            )
        },
    }
    inserted = 0
    for url, lang in ((ueim.TR_LISTING, "tr"), (ueim.EN_LISTING, "en")):
        time.sleep(ueim.DELAY)
        try:
            page_html = ueim.fetch_page(client, url)
        except Exception as e:  # noqa: BLE001
            print(f"[ueim discover] {url}: failed: {e}", file=sys.stderr)
            continue
        records = ueim.parse_listing(page_html, url, lang=lang)
        inserted += _insert_records(conn, records, taken)
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each status='new' row: download the PDF + pdftotext + reg verify."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, registration FROM ueim_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(ueim.DELAY)
        try:
            ueim.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:  # noqa: BLE001
            print(f"[ueim fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new'
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"  # no usable text layer

        registration = row["registration"] or \
            ueim.extract_registration_from_text(text)
        try:
            conn.execute(
                "UPDATE ueim_reports SET narrative_text=?, source_tier=?, "
                "registration=?, pdf_path=?, status=?, updated_at=? "
                "WHERE pdf_url=?",
                (text, tier, registration, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), pdf_url),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[ueim fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into ueim_accidents."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, page_url, lang, report_type, aircraft, "
        "registration, location, date_of_occurrence, narrative_text "
        "FROM ueim_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ueim_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO ueim_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, probable_cause, source_url, "
            "report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "TR",
                row["lang"],
                narrative,
                None,
                row["pdf_url"] or row["page_url"] or ueim.BASE,
                row["report_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ueim_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
