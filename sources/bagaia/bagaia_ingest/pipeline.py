# bagaia_ingest/pipeline.py
"""seed → fetch → parse → build pipeline for BAGAIA.

seed(): insert static SEED_REPORTS rows (idempotent).
discover(): query BAGAIA dashboard API for new non-member-state reports.
fetch(): download PDFs for status='new' rows.
parse(): extract text via pdftotext.
build(): emit bagaia_accidents rows with narrative >= floor.
"""
import os
import sys
import time

from . import bagaia, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

_NARRATIVE_FLOOR = 300


def seed(conn):
    """Insert static SEED_REPORTS rows.  Idempotent — existing case_ids skipped."""
    inserted = 0
    for row in bagaia.SEED_REPORTS:
        if conn.execute(
            "SELECT 1 FROM bagaia_reports WHERE case_id=?", (row["case_id"],)
        ).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO bagaia_reports "
            "(case_id, report_url, pdf_url, title, event_class, aircraft, "
            "registration, operator, date_of_occurrence, location, lang, "
            "status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("operator"),
                row.get("date_of_occurrence"),
                row.get("location"),
                row.get("lang", "en"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def discover(conn, client):
    """Query BAGAIA dashboard for new non-member-state candidates.

    Candidates already in the DB (by case_id or pdf_url) are skipped.
    Returns number of new rows inserted.
    """
    try:
        rows = bagaia.fetch_dashboard(client)
    except Exception as e:
        print(f"[bagaia discover] dashboard fetch failed: {e}", file=sys.stderr)
        return 0

    candidates = bagaia.dashboard_candidates(rows)
    inserted = 0
    for row in candidates:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM bagaia_reports WHERE case_id=? OR pdf_url=?",
            (case_id, row["pdf_url"]),
        ).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO bagaia_reports "
            "(case_id, report_url, pdf_url, title, event_class, aircraft, "
            "registration, operator, date_of_occurrence, location, lang, "
            "status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("operator"),
                row.get("date_of_occurrence"),
                row.get("location"),
                row.get("lang", "en"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """Download PDF for each status='new' row.  Failure keeps status='new'."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM bagaia_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        if not pdf_url:
            # No URL: advance to fetched with no path
            conn.execute(
                "UPDATE bagaia_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
            continue

        safe = case_id.replace("/", "_").replace(" ", "_")
        dest = os.path.join(pdf_dir, safe + ".pdf")
        try:
            time.sleep(bagaia.DELAY)
            resp = client.get(pdf_url)
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                fh.write(resp.content)
        except Exception as exc:
            print(f"[bagaia fetch] {case_id}: {exc}", file=sys.stderr)
            continue  # stay at 'new'

        conn.execute(
            "UPDATE bagaia_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
            (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()

    return len(rows)


def parse(conn):
    """Extract text from each status='fetched' PDF."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM bagaia_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()
    for row in rows:
        full_text = extract_text(row["pdf_path"]) if row["pdf_path"] else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif full_text:
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE bagaia_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()
    return len(rows)


def build(conn):
    """Emit bagaia_accidents rows for parsed rows with sufficient narrative."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM bagaia_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE bagaia_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO bagaia_accidents "
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
                "ST",  # São Tomé — state of occurrence for UR-CKC; future rows may differ
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE bagaia_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
