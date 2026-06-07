# jst_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for JST Argentina.

discover() paginates the aviation events API (modo=2) AND fetches the PDF
manifest (Index.json) once.  For each event it joins on the 8-digit
zero-padded expediente core (case_id); events whose expediente has at least
one manifest doc get a row, doc-less events are stubs and skipped (mirrors
knkt skipping report-less occurrences).  The chosen doc is picked by the
ISO > IB > INC > IPROV > IP preference; vehiculos[0] supplies aircraft /
registration / operator, victimas_fatales sums to fatalities, reseña is
stored as the summary.

fetch() downloads the chosen PDF, pdftotext, tiers pdf/scanned, advances to
'parsed'.  A download/extract failure leaves the row 'new' for retry.

build() promotes 'parsed' rows with narrative >= floor into jst_accidents
(country AR, source_url = the PDF URL, report_type = doc tipo).
"""
import os
import sys
import time

from . import db, jst, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, max_pages=None, full=False):
    """
    Paginate the events API + fetch the manifest once; INSERT new
    doc-bearing rows keyed on the 8-digit expediente core (case_id).
    """
    manifest = jst.fetch_manifest(client)
    existing = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM jst_reports")
    }
    inserted = 0
    pagina = 1
    while True:
        if max_pages is not None and pagina > max_pages:
            break
        events = jst.fetch_events_page(client, pagina)
        if not events:
            break
        for event in events:
            meta = jst.parse_event(event)
            case_id = meta["case_id"]
            if not case_id:
                continue
            docs = manifest.get(case_id)
            if not docs:
                continue  # doc-less event — stub, skip like knkt
            if case_id in existing:
                continue
            doc_path, doc_tipo = jst.pick_doc(docs)
            if not doc_path:
                continue
            existing.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO jst_reports "
                "(case_id, nro_expediente, doc_path, doc_tipo, aircraft, "
                "registration, operator, occurrence_type, date_of_occurrence, "
                "location, fatalities, summary, pdf_url, status, "
                "discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    meta["nro_expediente"],
                    doc_path,
                    doc_tipo,
                    meta["aircraft"],
                    meta["registration"],
                    meta["operator"],
                    meta["occurrence_type"],
                    meta["date"],
                    meta["location"],
                    meta["fatalities"],
                    meta["summary"],
                    jst.pdf_url(doc_path),
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        # paginate while pages still hold the full batch of 20
        if len(events) < 20:
            break
        pagina += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: download the chosen PDF, pdftotext, tier,
    advance to 'parsed'.  Failing rows stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, doc_path, pdf_url FROM jst_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        url = row["pdf_url"] or jst.pdf_url(row["doc_path"])
        if not url:
            continue
        time.sleep(jst.DELAY)
        try:
            jst.download_pdf(client, url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[jst fetch] {case_id}: {url}: {e}", file=sys.stderr)
            continue  # stays 'new' for retry next cycle

        tier = "pdf" if len(text or "") >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE jst_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (text, tier, pdf_path, db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[jst fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into jst_accidents."""
    rows = conn.execute(
        "SELECT case_id, doc_tipo, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, pdf_url, summary "
        "FROM jst_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE jst_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO jst_accidents "
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
                "AR",
                narrative,
                None,
                row["pdf_url"] or "https://www.argentina.gob.ar/jst",
                row["doc_tipo"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE jst_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
