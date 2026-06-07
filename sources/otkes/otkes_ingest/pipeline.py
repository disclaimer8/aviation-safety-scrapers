# otkes_ingest/pipeline.py
"""
discover → fetch → build pipeline for OTKES Finland.

⚠️ This source's metadata (Tutkintanumero / Onnettomuuspäivä / Finnish
summary) and the year-listing report links are ALL JS-injected, so discover()
does BOTH the listing walk AND the per-detail render in the same browser
session (throttled). fetch() then downloads the report PDF via PLAIN httpx
(static files, no cookies). build() promotes qualifying rows.

  discover(conn, browser):        render listings + details → insert rows
  fetch(conn, client, pdf_dir):   httpx-download PDFs, pdftotext → narrative
  build(conn):                    promote into otkes_accidents (country FI)

build() qualifies a row on EITHER a usable PDF text layer OR a sufficiently
long rendered page summary (many lighter 'selvitys' reports have no PDF — the
on-page Finnish summary IS the narrative).
"""
import os
import sys
import time

from . import otkes, db, pdf
from .text import make_site_slug

# build() floor: a row needs narrative >= this to become an accident.
_NARRATIVE_FLOOR = 300


def _insert_detail(conn, detail_url, year, meta, taken):
    """INSERT one rendered detail into otkes_reports. Returns 1 if inserted."""
    if conn.execute(
        "SELECT 1 FROM otkes_reports WHERE detail_url=?", (detail_url,)
    ).fetchone():
        return 0

    case_id = otkes.normalize_case_number(meta.get("case_number"))
    if not case_id:
        case_id = otkes.fallback_case_id(detail_url)
    # case_id collision guard (legacy ids occasionally repeat across pages)
    base = case_id
    n = 2
    while case_id in taken:
        case_id = f"{base}-{n}"
        n += 1
    taken.add(case_id)

    ts = db.now_ms()
    conn.execute(
        "INSERT INTO otkes_reports "
        "(case_id, detail_url, pdf_url, year, title, occurrence_type, "
        " registration, event_date, publish_date, page_summary, lang, "
        " status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            detail_url,
            meta.get("pdf_url"),
            year,
            meta.get("title"),
            meta.get("occurrence_type"),
            meta.get("registration"),
            meta.get("event_date"),
            meta.get("publish_date"),
            meta.get("summary"),
            "fi",
            db.STATUS_NEW,
            ts,
            ts,
        ),
    )
    return 1


def discover(conn, browser, full=False, max_listings=None, max_details=None):
    """Render every aviation listing, then render each new detail page and
    INSERT it. Returns count of newly inserted rows.

    max_listings / max_details cap the work for smoke runs.
    """
    taken = {
        r["case_id"]
        for r in conn.execute("SELECT case_id FROM otkes_reports")
    }
    known_details = {
        r["detail_url"]
        for r in conn.execute(
            "SELECT detail_url FROM otkes_reports WHERE detail_url IS NOT NULL"
        )
    }

    try:
        listings = browser.harvest_listings()
    except Exception as exc:
        print(f"[otkes discover] root harvest failed: {exc}", file=sys.stderr)
        return 0
    if max_listings:
        listings = listings[:max_listings]

    # Collect detail URLs across all listings first (dedupe across year/topic).
    detail_to_year = {}
    for listing_url in listings:
        year = otkes.year_from_year_url(listing_url)
        time.sleep(otkes.DELAY)
        try:
            details = browser.get_detail_urls(listing_url, year)
        except Exception as exc:
            print(f"[otkes discover] listing {listing_url}: {exc}",
                  file=sys.stderr)
            continue
        for d in details:
            if d not in detail_to_year:
                detail_to_year[d] = year or otkes.year_from_year_url(d)

    new_details = [d for d in detail_to_year if d not in known_details]
    if max_details:
        new_details = new_details[:max_details]

    inserted = 0
    for detail_url in new_details:
        year = detail_to_year[detail_url]
        time.sleep(otkes.DELAY)
        try:
            meta = browser.get_detail(detail_url)
        except Exception as exc:
            print(f"[otkes discover] detail {detail_url}: {exc}",
                  file=sys.stderr)
            continue
        try:
            inserted += _insert_detail(conn, detail_url, year, meta, taken)
            conn.commit()
        except Exception as exc:
            print(f"[otkes discover] insert {detail_url}: {exc}",
                  file=sys.stderr)
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """Download each new row's report PDF (httpx) + pdftotext → narrative.

    Rows with no pdf_url keep their rendered page_summary as the narrative
    (tier 'summary'). Returns number of rows processed.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, detail_url, pdf_url, page_summary "
        "FROM otkes_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        page_summary = row["page_summary"] or ""
        narrative = ""
        tier = "none"
        pdf_path = None

        if pdf_url:
            safe = case_id.replace("/", "_").replace(" ", "_")
            pdf_path = os.path.join(pdf_dir, f"{safe}.pdf")
            time.sleep(otkes.DELAY)
            try:
                otkes.download_pdf(client, pdf_url, pdf_path)
                text = pdf.extract_text(pdf_path)
                if len(text) >= pdf.MIN_NARRATIVE:
                    narrative, tier = text, "pdf"
                else:
                    # scanned / thin PDF — keep whichever is longer between the
                    # (thin) PDF text and the rendered page summary.
                    narrative = text if len(text) >= len(page_summary) else page_summary
                    tier = "scanned"
            except Exception as exc:
                print(f"[otkes fetch] {case_id}: pdf {exc}", file=sys.stderr)
                pdf_path = None
                narrative, tier = page_summary, "summary"
        else:
            narrative, tier = page_summary, "summary"

        # best-effort registration from PDF text if not already set
        reg = otkes.extract_registration(narrative)
        try:
            conn.execute(
                "UPDATE otkes_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, registration=COALESCE(registration, ?), "
                "status=?, updated_at=? WHERE detail_url=?",
                (narrative, tier, pdf_path, reg, db.STATUS_PARSED,
                 db.now_ms(), row["detail_url"]),
            )
            conn.commit()
        except Exception as exc:
            print(f"[otkes fetch] {case_id}: db {exc}", file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote parsed rows with narrative >= floor into otkes_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, title, occurrence_type, registration, "
        "event_date, narrative_text, source_tier "
        "FROM otkes_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE otkes_reports SET status=?, updated_at=? "
                "WHERE detail_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["detail_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["title"], row["registration"], None
        )
        conn.execute(
            "INSERT OR REPLACE INTO otkes_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["event_date"],
                None,
                row["registration"],
                None,
                None,
                "FI",
                narrative,
                None,
                row["detail_url"] or otkes.BASE,
                row["occurrence_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE otkes_reports SET status=?, updated_at=? WHERE detail_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["detail_url"]),
        )
        conn.commit()
        built += 1
    return built
