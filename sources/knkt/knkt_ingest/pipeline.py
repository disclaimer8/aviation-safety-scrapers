# knkt_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for KNKT / NTSC Indonesia.

discover() GETs the single JSON listing, keeps only report-bearing rows
(Final > Interim > Preliminary), parses Keterangan metadata, inserts new
rows keyed on case_id (KNKT case number, else reg+date).

fetch() tries each candidate PDF URL (occurrence-year folder first, then
case-number-year — the verified folder-year trap), pdftotext, advances to
'parsed'.  All-candidates-404 rows stay 'new' for retry next cycle.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
knkt_accidents (country ID).
"""
import os
import sys
import time

from . import db, knkt, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """Fetch the JSON listing; INSERT new report-bearing rows."""
    rows = knkt.fetch_listing(client)
    taken = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM knkt_reports")
    }
    inserted = 0
    for row in rows:
        filename, kind = knkt.pick_report(row)
        if not filename:
            continue  # occurrence stub, no published report
        if conn.execute(
            "SELECT 1 FROM knkt_reports WHERE report_file=?", (filename,)
        ).fetchone():
            continue
        meta = knkt.parse_keterangan(row.get("Keterangan"))
        date = (row.get("Tanggal") or "")[:10] or None
        case_id = knkt.make_case_id(
            meta["case_id"], meta["registration"], date, taken=taken
        )
        taken.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO knkt_reports "
            "(case_id, report_file, report_kind, aircraft, registration, "
            "operator, occurrence_type, date_of_occurrence, location, "
            "status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                filename,
                kind,
                meta["aircraft"],
                meta["registration"],
                meta["operator"],
                meta["occurrence_type"],
                date,
                meta["location"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: try candidate PDF URLs (folder-year trap),
    pdftotext, advance to 'parsed'.  Failing rows stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, report_file, date_of_occurrence FROM knkt_reports "
        "WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        safe = case_id.replace("/", "_").replace(" ", "_")
        pdf_path = os.path.join(pdf_dir, f"{safe}.pdf")
        urls = knkt.candidate_pdf_urls(
            row["date_of_occurrence"], row["report_file"], case_id
        )
        text = None
        used_url = None
        for url in urls:
            time.sleep(knkt.DELAY)
            try:
                knkt.download_pdf(client, url, pdf_path)
                text = pdf.extract_text(pdf_path)
                used_url = url
                break
            except Exception as e:
                print(f"[knkt fetch] {case_id}: {url}: {e}", file=sys.stderr)
        if used_url is None:
            continue  # all candidates failed — stays 'new' for retry

        tier = "pdf" if len(text or "") >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE knkt_reports SET narrative_text=?, source_tier=?, "
                "pdf_url=?, pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (text, tier, used_url, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[knkt fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into knkt_accidents."""
    rows = conn.execute(
        "SELECT case_id, report_kind, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, pdf_url "
        "FROM knkt_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE knkt_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO knkt_accidents "
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
                "ID",
                narrative,
                None,
                row["pdf_url"] or "https://knkt.go.id/",
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE knkt_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
