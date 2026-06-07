# uzpln_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for UZPLN (Czech Republic).

discover() walks the paginated listing  /zpravy-ln?page=N  until the STOP
SIGNAL (a page with ZERO /incident/ links — NOT a 404). For each new listing
row it GETs the /incident/{id} detail page to pull the narrative PDF href
(spaces/diacritics URL-encoded) + aircraft type, then INSERTs a row keyed on
case_id (CZ-YY-NNNN, or 'uzpln-{id}' surrogate when the report number is
absent/duplicate). incident_id is the de-dupe key, so the per-row detail GET
is skipped on re-runs.

fetch() downloads each new PDF + pdftotext (tier 'pdf'), extracts the OK-
registration best-effort from the text; a PDF with no usable text layer is
tiered 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
uzpln_accidents (country CZ, lang cs).
"""
import os
import sys
import time

from . import uzpln, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False, max_pages=None):
    """Walk listing pages until the stop signal; INSERT new rows. Returns count."""
    if max_pages is None:
        max_pages = uzpln.MAX_PAGES
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM uzpln_reports WHERE case_id IS NOT NULL"
        )
    }
    seen_incident = {
        r["incident_id"]
        for r in conn.execute(
            "SELECT incident_id FROM uzpln_reports WHERE incident_id IS NOT NULL"
        )
    }
    inserted = 0
    empty_streak = 0  # consecutive link-less pages (transient blanks tolerated)
    for page in range(max_pages):
        list_url = uzpln.list_url(page)
        time.sleep(uzpln.DELAY)
        try:
            list_html = uzpln.fetch_page(client, list_url)
        except Exception as e:
            print(f"[uzpln discover] {list_url}: failed: {e}", file=sys.stderr)
            continue

        if not uzpln.has_incident_links(list_html):
            # A link-less page is EITHER the true end-of-catalogue stop page OR
            # a transient blank served under load (the interleaved detail GETs
            # double the request rate and occasionally trip rate-limiting).
            # Re-fetch after a longer pause: a load-blank recovers, the true
            # stop page stays blank. Only count CONFIRMED blanks toward the
            # EMPTY_STREAK_STOP halt.
            confirmed_blank = True
            for _ in range(uzpln.BLANK_RETRIES):
                time.sleep(uzpln.DELAY * uzpln.BLANK_RETRY_BACKOFF)
                try:
                    retry_html = uzpln.fetch_page(client, list_url)
                except Exception as e:
                    print(f"[uzpln discover] {list_url}: retry failed: {e}",
                          file=sys.stderr)
                    continue
                if uzpln.has_incident_links(retry_html):
                    list_html = retry_html
                    confirmed_blank = False
                    break
            if confirmed_blank:
                empty_streak += 1
                if empty_streak >= uzpln.EMPTY_STREAK_STOP:
                    break
                continue
        empty_streak = 0

        for rec in uzpln.parse_listing(list_html):
            incident_id = rec["incident_id"]
            if incident_id in seen_incident:
                continue
            seen_incident.add(incident_id)

            time.sleep(uzpln.DELAY)
            try:
                detail_html = uzpln.fetch_page(client, rec["detail_url"])
            except Exception as e:
                print(f"[uzpln discover] {rec['detail_url']}: detail failed: {e}",
                      file=sys.stderr)
                seen_incident.discard(incident_id)  # retry next cycle
                continue
            det = uzpln.parse_detail(detail_html)

            pdf_url = uzpln.encode_pdf_href(det["pdf_href"])
            # Prefer detail-page metadata, fall back to the listing row.
            report_number = det["report_number"] or rec["report_number"]
            event_date = det["event_date"] or rec["event_date"]
            report_kind = det["report_kind"] or rec["report_kind"]
            event_kind = det["event_kind"] or rec["event_kind"]
            operation = det["operation"] or rec["operation"]
            location = det["location"] or rec["location"]
            aircraft = det["aircraft"]

            case_id = uzpln.make_case_id(report_number, incident_id, taken=taken)
            taken.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO uzpln_reports "
                "(case_id, incident_id, pdf_url, page_url, report_number, "
                "report_kind, event_kind, operation, aircraft, registration, "
                "date_of_occurrence, location, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    incident_id,
                    pdf_url,
                    rec["detail_url"],
                    report_number,
                    report_kind,
                    event_kind,
                    operation,
                    aircraft,
                    None,  # registration filled at fetch from PDF text
                    event_date,
                    location,
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
        "SELECT case_id, pdf_url FROM uzpln_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        if not pdf_url:
            # No PDF on the detail page → nothing to ingest; park it.
            conn.execute(
                "UPDATE uzpln_reports SET status=?, source_tier=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, "no-pdf", db.now_ms(), case_id),
            )
            conn.commit()
            continue
        safe = case_id.replace("/", "_")
        pdf_path = os.path.join(pdf_dir, f"{safe}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(uzpln.DELAY)
        try:
            uzpln.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[uzpln fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new'
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"  # no usable text layer

        registration = uzpln.extract_registration(text)
        try:
            conn.execute(
                "UPDATE uzpln_reports SET narrative_text=?, source_tier=?, "
                "registration=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (text, tier, registration, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[uzpln fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into uzpln_accidents."""
    rows = conn.execute(
        "SELECT case_id, page_url, report_kind, aircraft, registration, "
        "location, date_of_occurrence, narrative_text "
        "FROM uzpln_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE uzpln_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO uzpln_accidents "
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
                "CZ",
                "cs",
                narrative,
                None,
                row["page_url"] or uzpln.BASE,
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE uzpln_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
