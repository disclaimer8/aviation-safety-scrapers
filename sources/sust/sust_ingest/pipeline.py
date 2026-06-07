# sust_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for SUST / STSB Switzerland.

discover() GETs the ONE skeleton page (cHashes baked there), parses every
data-lazyload URL, and INSERTs new rows keyed on uid (case_id=str(uid)) with
the lazyload_url.  CHEAP — no per-row network; metadata comes at fetch.

fetch() GETs each 'new' row's getEntry JSON, parses metadata, picks the best
document (Schlussbericht > … > Notification), downloads the PDF verbatim from
documents[].url, pdftotext, tiers pdf/scanned, advances to 'parsed'.  Rows
whose JSON has NO documents keep their metadata but STAY 'new' — so the weekly
cycle self-heals when a report is later published (shk-style).  Network/PDF
failures also stay 'new' for retry.

build() promotes 'parsed' rows with narrative >= floor into sust_accidents
(country CH).
"""
import os
import sys
import time

from . import db, sust, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """Step 1: GET skeleton; INSERT new (uid, lazyload_url) rows. Idempotent."""
    rows = sust.fetch_skeleton(client)
    existing = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM sust_reports")
    }
    inserted = 0
    for uid, lazyload_url in rows:
        case_id = str(uid)
        if case_id in existing:
            continue
        existing.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO sust_reports "
            "(case_id, lazyload_url, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (case_id, lazyload_url, db.STATUS_NEW, ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs", max_rows=None):
    """
    Step 2: for each 'new' row, GET getEntry JSON → metadata + doc choice;
    download the chosen PDF verbatim; pdftotext; advance to 'parsed'.
    Doc-less rows keep metadata but stay 'new' (weekly self-heal).  Failures
    stay 'new'.  --max-rows caps the count (smoke).
    """
    q = (
        "SELECT case_id, lazyload_url FROM sust_reports WHERE status=? "
        "ORDER BY CAST(case_id AS INTEGER) DESC"
    )
    params = (db.STATUS_NEW,)
    if max_rows is not None:
        q += " LIMIT ?"
        params = (db.STATUS_NEW, max_rows)
    rows = conn.execute(q, params).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    processed = 0
    for row in rows:
        case_id = row["case_id"]
        processed += 1
        time.sleep(sust.DELAY)
        try:
            meta = sust.fetch_entry(client, row["lazyload_url"])
        except Exception as e:
            print(f"[sust fetch] {case_id}: entry GET failed: {e}",
                  file=sys.stderr)
            continue

        doc = meta["doc"]
        # Persist metadata regardless (so a self-heal cycle skips re-parsing
        # the fields), but keep doc-less rows 'new' for future publication.
        base_fields = (
            meta["aircraft"], meta["registration"], meta["operator"],
            meta["occurrence_type"], meta["date_of_occurrence"],
            meta["location"],
        )
        if doc is None:
            conn.execute(
                "UPDATE sust_reports SET aircraft=?, registration=?, "
                "operator=?, occurrence_type=?, date_of_occurrence=?, "
                "location=?, updated_at=? WHERE case_id=?",
                (*base_fields, db.now_ms(), case_id),
            )
            conn.commit()
            continue  # no published report yet — stays 'new'

        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        time.sleep(sust.DELAY)
        try:
            sust.download_pdf(client, doc["url"], pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[sust fetch] {case_id}: PDF {doc['url']}: {e}",
                  file=sys.stderr)
            # store metadata, stay 'new' for retry
            conn.execute(
                "UPDATE sust_reports SET aircraft=?, registration=?, "
                "operator=?, occurrence_type=?, date_of_occurrence=?, "
                "location=?, doc_name=?, lang=?, pdf_url=?, updated_at=? "
                "WHERE case_id=?",
                (*base_fields, doc["name"], doc["lang"], doc["url"],
                 db.now_ms(), case_id),
            )
            conn.commit()
            continue

        tier = "pdf" if len(text or "") >= _NARRATIVE_FLOOR else "scanned"
        conn.execute(
            "UPDATE sust_reports SET aircraft=?, registration=?, operator=?, "
            "occurrence_type=?, date_of_occurrence=?, location=?, doc_name=?, "
            "lang=?, narrative_text=?, source_tier=?, pdf_url=?, pdf_path=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (*base_fields, doc["name"], doc["lang"], text, tier, doc["url"],
             pdf_path, db.STATUS_PARSED, db.now_ms(), case_id),
        )
        conn.commit()
    return processed


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into sust_accidents."""
    rows = conn.execute(
        "SELECT case_id, doc_name, lang, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, pdf_url "
        "FROM sust_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE sust_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO sust_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, probable_cause, source_url, "
            "report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "CH",
                row["lang"],
                narrative,
                None,
                row["pdf_url"] or "https://www.sust.admin.ch/",
                row["doc_name"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE sust_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
