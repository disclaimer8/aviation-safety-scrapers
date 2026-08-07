# ntsbaar_ingest/pipeline.py
"""discover → fetch → parse → build for the NTSB Aircraft Accident Reports.

discover(): read the Hunt Library index, INSERT new report numbers. An index
  that serves bytes but yields no reports raises — the collection has 415 and
  will not empty, so zero means the page changed.

fetch(): plain httpx from ntsb.gov. A failure keeps the row at 'new'.

parse(): text layer first, OCR second. About a third of these have a real text
  layer; the rest are scans of typewritten pages.

build(): project into ntsbaar_accidents. Only the narrative is written — the
  event date, aircraft and registration live inside the report prose and need
  their own extractor, which is not a regex written on a guess.
"""
import os
import sys
import time

from . import db, ntsbaar
from .pdf import extract_text, ocr_extract

_NARRATIVE_FLOOR = 1000  # an AAR is a long document; less than this is a scan
_TEXT_LAYER_FLOOR = 5000  # below this the "text layer" is page furniture


def discover(conn, client):
    """Read the index; INSERT new reports. Returns inserted count."""
    resp = client.get(ntsbaar.INDEX_URL, headers=ntsbaar.HEADERS)
    resp.raise_for_status()
    rows = ntsbaar.parse_index(resp.text)
    if not rows:
        # Discovery leans on a third party's index page, so it has to be loud
        # when that page changes. Silence here would look like a clean run.
        raise RuntimeError(
            f"[ntsbaar discover] {ntsbaar.INDEX_URL} served "
            f"{len(resp.text)} bytes but yielded 0 report links — "
            "the index markup or its NTSB links have changed")

    inserted = 0
    for r in rows:
        if conn.execute("SELECT 1 FROM ntsbaar_reports WHERE case_id=?",
                        (r["case_id"],)).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ntsbaar_reports (case_id, pdf_url, adopted_year, "
            "status, discovered_at, updated_at) VALUES (?,?,?,?,?,?)",
            (r["case_id"], r["pdf_url"], r["adopted_year"], db.STATUS_NEW,
             ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """Download each 'new' report from ntsb.gov. Returns rows seen."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ntsbaar_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        dest = os.path.join(pdf_dir, f"{case_id}.pdf")
        time.sleep(ntsbaar.DELAY)
        try:
            size = ntsbaar.download(client, row["pdf_url"], dest)
        except Exception as exc:
            # Stay 'new': a download we could not complete is a transient
            # fault, and only 'new' is ever retried.
            print(f"[ntsbaar fetch] {case_id}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        conn.execute(
            "UPDATE ntsbaar_reports SET pdf_path=?, pdf_bytes=?, status=?, "
            "updated_at=? WHERE case_id=?",
            (dest, size, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()

    return len(rows)


def parse(conn, enable_ocr=True):
    """Text layer first, OCR second. Returns rows processed."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ntsbaar_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        path = row["pdf_path"]
        text = extract_text(path) if path else ""
        tier = "pdf"

        # A scan still yields a few dozen characters of noise from stamps and
        # page numbers, so "has some text" is not the test — "has enough to be
        # a report" is.
        if len(text) < _TEXT_LAYER_FLOOR and enable_ocr and path \
                and os.path.exists(path):
            ocr = ocr_extract(path, lang=ntsbaar.OCR_LANG)
            if len(ocr) > len(text):
                text, tier = ocr, "ocr"

        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"

        conn.execute(
            "UPDATE ntsbaar_reports SET narrative_text=?, source_tier=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (text, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Project 'parsed' rows into ntsbaar_accidents. Returns rows built."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, adopted_year, narrative_text "
        "FROM ntsbaar_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ntsbaar_reports SET status=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        conn.execute(
            "INSERT OR REPLACE INTO ntsbaar_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                # NOT adopted_year: the Board adopts a report a year or two
                # after the accident, so using it as the event date would
                # misdate most of the collection. The real date is in the
                # prose and needs its own extractor.
                None,
                None, None, None, None,
                "US",
                narrative,
                None,
                row["pdf_url"],
                "Aircraft Accident Report",
                f"ntsb-{row['case_id'].lower()}",
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ntsbaar_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
