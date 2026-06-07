# shk_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for SHK Sweden.

discover() pulls the sitemap, keeps aviation detail URLs, inserts new rows
(case_id = slug minus the 2023-11 migration-date prefix).

fetch() GETs the detail page, extracts metadata + the best report PDF
(full-EN > EN Summary > Swedish full — SV handled by the Phase-3 SV→EN
rewrite, the NSIA/BFU precedent), downloads + pdftotext.  Ongoing
investigations (no PDF) stay at 'new' and self-heal on the weekly cycle
when the report publishes.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
shk_accidents (country SE).
"""
import os
import sys
import time

from . import db, pdf, shk
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """Sitemap → INSERT new aviation rows. Returns inserted count."""
    xml = shk.fetch_sitemap(client)
    urls = shk.parse_sitemap(xml)
    taken = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM shk_reports")
    }
    inserted = 0
    for url in urls:
        if conn.execute(
            "SELECT 1 FROM shk_reports WHERE detail_url=?", (url,)
        ).fetchone():
            continue
        case_id = shk.case_id_from_url(url, taken=taken)
        taken.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO shk_reports (case_id, detail_url, status, "
            "discovered_at, updated_at) VALUES (?,?,?,?,?)",
            (case_id, url, db.STATUS_NEW, ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: GET the detail page; if it has a report PDF
    → download + pdftotext → 'parsed'.  No PDF (ongoing investigation) →
    metadata stored, row STAYS 'new' for the weekly self-heal.
    """
    rows = conn.execute(
        "SELECT case_id, detail_url FROM shk_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(shk.DELAY)
        try:
            html = shk.fetch_page(client, row["detail_url"])
            d = shk.parse_detail(html)
        except Exception as e:
            print(f"[shk fetch] {case_id}: page failed: {e}", file=sys.stderr)
            continue

        if not d["pdf_href"]:
            # ongoing — keep metadata, stay 'new' (self-heals when published)
            conn.execute(
                "UPDATE shk_reports SET title=?, registration=?, "
                "date_of_occurrence=?, diarienummer=?, updated_at=? "
                "WHERE case_id=?",
                (d["title"], d["registration"], d["event_date"],
                 d["diarienummer"], db.now_ms(), case_id),
            )
            conn.commit()
            continue

        pdf_path = os.path.join(pdf_dir, f"{case_id[:60]}.pdf")
        try:
            time.sleep(shk.DELAY)
            shk.download_pdf(client, d["pdf_href"], pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[shk fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue

        tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        try:
            conn.execute(
                "UPDATE shk_reports SET title=?, registration=?, "
                "date_of_occurrence=?, diarienummer=?, rl_number=?, "
                "report_kind=?, lang=?, narrative_text=?, source_tier=?, "
                "pdf_url=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (
                    d["title"], d["registration"], d["event_date"],
                    d["diarienummer"], d["rl_number"], d["report_kind"],
                    d["lang"], text, tier,
                    shk.BASE + d["pdf_href"], pdf_path,
                    db.STATUS_PARSED, db.now_ms(), case_id,
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"[shk fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into shk_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, title, report_kind, registration, "
        "date_of_occurrence, narrative_text FROM shk_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE shk_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(None, row["registration"], row["title"])
        conn.execute(
            "INSERT OR REPLACE INTO shk_accidents "
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
                "SE",
                narrative,
                None,
                row["detail_url"] or "https://shk.se/",
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE shk_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
