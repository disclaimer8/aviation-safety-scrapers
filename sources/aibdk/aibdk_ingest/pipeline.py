# aibdk_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for AIB Denmark.

discover() GETs ONE year page and harvests the full case list from the
filter-checkbox leak (~428 ids).

fetch() resolves each case's detail URL via the filing-year cascade
(case-year, ±1, sweep — the /2015/2018-401 trap), parses the title
metadata + CDN PDF, downloads (PDFs up to 34MB) + pdftotext.  Cases that
don't resolve or lack a PDF stay 'new' for the weekly retry.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
aibdk_accidents (country DK).  Danish text → DA→EN at Phase 3.
"""
import os
import sys
import time

from . import aibdk, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """One year-page GET → INSERT all leaked case_ids. Returns inserted."""
    html = aibdk.fetch_year_page(client, 2023)
    ids = aibdk.parse_case_ids(html)
    inserted = 0
    for case_id in ids:
        if conn.execute(
            "SELECT 1 FROM aibdk_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aibdk_reports (case_id, status, discovered_at, "
            "updated_at) VALUES (?,?,?,?)",
            (case_id, db.STATUS_NEW, ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: resolve detail URL (year cascade), parse,
    download the Danish PDF, pdftotext.  Unresolvable/PDF-less stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, detail_url FROM aibdk_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        url = row["detail_url"]
        if url:
            time.sleep(aibdk.DELAY)
            try:
                html = aibdk.fetch_page(client, url)
            except Exception:
                url = None
        if not url:
            url, html = aibdk.resolve_detail(client, case_id)
        if not url:
            # full year-sweep failed — the case has no public page (old cases
            # often don't). Mark 'missing' so the weekly cycle doesn't burn
            # ~a minute re-sweeping it; discover won't re-insert (case_id PK).
            print(f"[aibdk fetch] {case_id}: unresolved -> missing", file=sys.stderr)
            conn.execute(
                "UPDATE aibdk_reports SET status='missing', updated_at=? "
                "WHERE case_id=?",
                (db.now_ms(), case_id),
            )
            conn.commit()
            continue

        d = aibdk.parse_case(html)
        if not d["pdf_url"]:
            # report not published yet — keep what we know, stay 'new'
            conn.execute(
                "UPDATE aibdk_reports SET detail_url=?, title=?, "
                "registration=?, date_of_occurrence=?, location=?, "
                "updated_at=? WHERE case_id=?",
                (url, d["title"], d["registration"], d["event_date"],
                 d["location"], db.now_ms(), case_id),
            )
            conn.commit()
            continue

        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        try:
            time.sleep(aibdk.DELAY)
            aibdk.download_pdf(client, d["pdf_url"], pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[aibdk fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue

        tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE aibdk_reports SET detail_url=?, title=?, "
                "registration=?, date_of_occurrence=?, location=?, lang=?, "
                "narrative_text=?, source_tier=?, pdf_url=?, pdf_path=?, "
                "status=?, updated_at=? WHERE case_id=?",
                (
                    url, d["title"], d["registration"], d["event_date"],
                    d["location"], "da", text, tier, d["pdf_url"], pdf_path,
                    db.STATUS_PARSED, db.now_ms(), case_id,
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"[aibdk fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into aibdk_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, title, registration, location, "
        "date_of_occurrence, narrative_text FROM aibdk_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aibdk_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(None, row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO aibdk_accidents "
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
                row["location"] or row["title"],
                "DK",
                narrative,
                None,
                row["detail_url"] or "https://en.havarikommissionen.dk/",
                None,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aibdk_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
