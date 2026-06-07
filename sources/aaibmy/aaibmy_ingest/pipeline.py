# aaibmy_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for AAIB Malaysia.

discover() GETs the hub, enumerates year child pages from its actual hrefs
(old years carry a literal 'd' suffix), GETs each year page, extracts the
EN-only PDF links (deduped by report number against the Malay copies),
parses filename metadata, and INSERTs new rows keyed on pdf_url
(case_id = report number, else slugified filename, collision-suffixed).

fetch() downloads each new PDF + pdftotext (tier 'pdf'); a PDF with no
usable text layer is tiered 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
aaibmy_accidents (country MY).
"""
import os
import sys
import time

from . import aaibmy, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """Walk hub → year pages; INSERT new rows. Returns inserted count."""
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM aaibmy_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0
    time.sleep(aaibmy.DELAY)
    try:
        hub_html = aaibmy.fetch_hub(client)
    except Exception as e:
        print(f"[aaibmy discover] hub failed: {e}", file=sys.stderr)
        return 0

    for year_url in aaibmy.year_links(hub_html):
        year = year_url.rsplit("/", 1)[-1]
        time.sleep(aaibmy.DELAY)
        try:
            year_html = aaibmy.fetch_page(client, year_url)
        except Exception as e:
            print(f"[aaibmy discover] year {year}: failed: {e}",
                  file=sys.stderr)
            continue
        for pdf_url, filename in aaibmy.pdf_links(year_html):
            if conn.execute(
                "SELECT 1 FROM aaibmy_reports WHERE pdf_url=?", (pdf_url,)
            ).fetchone():
                continue
            meta = aaibmy.parse_filename(filename)
            case_id = aaibmy.make_case_id(meta["case_id"], filename, taken=taken)
            taken.add(case_id)
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaibmy_reports "
                "(pdf_url, case_id, page_url, year, title, report_kind, "
                "occurrence_type, aircraft, registration, date_of_occurrence, "
                "location, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pdf_url,
                    case_id,
                    year_url,
                    year.rstrip("d"),
                    filename,
                    meta["report_kind"],
                    meta["occurrence_type"],
                    None,
                    meta["registration"],
                    None,
                    None,
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each status='new' row: download the PDF + pdftotext."""
    rows = conn.execute(
        "SELECT pdf_url, case_id FROM aaibmy_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(aaibmy.DELAY)
        try:
            aaibmy.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[aaibmy fetch] {case_id}: pdf failed: {e}",
                  file=sys.stderr)
            continue  # stays 'new'
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"  # no usable text layer

        try:
            conn.execute(
                "UPDATE aaibmy_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, status=?, updated_at=? WHERE pdf_url=?",
                (text, tier, pdf_path, db.STATUS_PARSED, db.now_ms(), pdf_url),
            )
            conn.commit()
        except Exception as e:
            print(f"[aaibmy fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into aaibmy_accidents."""
    rows = conn.execute(
        "SELECT pdf_url, case_id, page_url, report_kind, occurrence_type, "
        "aircraft, registration, location, date_of_occurrence, narrative_text "
        "FROM aaibmy_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaibmy_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        report_type = row["report_kind"] or row["occurrence_type"]
        conn.execute(
            "INSERT OR REPLACE INTO aaibmy_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "MY",
                narrative,
                None,
                row["page_url"] or aaibmy.HUB_URL,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaibmy_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
