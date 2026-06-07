# sub_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for SUB Austria.

discover() GETs the hub, then each category page. Year-based categories
(year links present) → GET each year page; flat categories → reports are on
the category page itself. Each report-detail page is GET'd and parsed for
metadata (date, aircraft, GZ, location, HTML summary, report-type, PDF URL).
case_id is derived from the report's relative path (path-based, unique
231/231). New rows are INSERTed keyed on case_id (page_url UNIQUE).

fetch() downloads each new report PDF + pdftotext (tier 'pdf'), extracts the
OE- registration best-effort; a PDF with no usable text layer is tiered
'scanned'.

build() promotes 'parsed' rows into sub_accidents (country 'AT', lang 'de').
narrative = PDF text when >= floor; otherwise FALL BACK to the stored HTML
summary when that is >= floor; else the row is skipped.
"""
import os
import sys
import time

from . import sub, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def _insert_report(conn, taken, url, meta):
    """INSERT one report row from its detail-page meta. Returns 1 if inserted."""
    case_id = sub.case_id_from_url(url)
    if case_id in taken:
        return 0
    if conn.execute(
        "SELECT 1 FROM sub_reports WHERE page_url=?", (url,)
    ).fetchone():
        taken.add(case_id)
        return 0
    taken.add(case_id)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO sub_reports "
        "(case_id, page_url, pdf_url, category, year, case_number, title, "
        "report_kind, aircraft, registration, date_of_occurrence, location, "
        "summary_text, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            url,
            meta.get("pdf_url"),
            sub.category_of(case_id),
            sub.year_of(case_id),
            meta.get("gz"),
            meta.get("aircraft"),
            meta.get("report_kind"),
            meta.get("aircraft"),
            None,  # registration filled at fetch from PDF text
            meta.get("event_date"),
            meta.get("location"),
            meta.get("summary_text"),
            db.STATUS_NEW,
            ts,
            ts,
        ),
    )
    return 1


def discover(conn, client, full=False):
    """Walk hub → categories → (years|flat) → report pages; INSERT new rows.
    Returns count inserted."""
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM sub_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0

    time.sleep(sub.DELAY)
    try:
        hub_html = sub.fetch_page(client, sub._hub_url())
    except Exception as e:
        print(f"[sub discover] hub failed: {e}", file=sys.stderr)
        return 0
    cats = sub.parse_hub(hub_html) or list(sub.CATEGORIES)

    for cat in cats:
        time.sleep(sub.DELAY)
        try:
            cat_html = sub.fetch_page(client, sub.category_url(cat))
        except Exception as e:
            print(f"[sub discover] category {cat} failed: {e}", file=sys.stderr)
            continue

        year_links = sub.parse_year_links(cat, cat_html)
        if year_links:
            # YEAR-BASED category.
            for _year, year_url in year_links:
                time.sleep(sub.DELAY)
                try:
                    year_html = sub.fetch_page(client, year_url)
                except Exception as e:
                    print(f"[sub discover] {year_url} failed: {e}",
                          file=sys.stderr)
                    continue
                inserted += _discover_reports(conn, client, taken, cat, year_html)
                conn.commit()
        else:
            # FLAT category: reports live on the category page.
            inserted += _discover_reports(conn, client, taken, cat, cat_html)
            conn.commit()

    return inserted


def _discover_reports(conn, client, taken, cat, listing_html):
    """For every report link on a listing page, GET its detail page, parse,
    and INSERT. Returns count inserted."""
    inserted = 0
    report_urls = sub.parse_report_links(cat, listing_html)
    for url in report_urls:
        case_id = sub.case_id_from_url(url)
        if case_id in taken:
            continue
        if conn.execute(
            "SELECT 1 FROM sub_reports WHERE page_url=?", (url,)
        ).fetchone():
            taken.add(case_id)
            continue
        time.sleep(sub.DELAY)
        try:
            page_html = sub.fetch_page(client, url)
        except Exception as e:
            print(f"[sub discover] report {url} failed: {e}", file=sys.stderr)
            continue
        meta = sub.parse_report(page_html)
        inserted += _insert_report(conn, taken, url, meta)
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """For each status='new' row WITH a pdf_url: download + pdftotext + reg.
    Rows without a PDF are left 'new' only if a summary is also unavailable;
    otherwise they go to 'parsed' so build() can use the HTML summary."""
    rows = conn.execute(
        "SELECT case_id, page_url, pdf_url, summary_text "
        "FROM sub_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        text = ""
        tier = "pdf"
        pdf_path = None

        if pdf_url:
            pdf_path = os.path.join(pdf_dir, f"{case_id}.pdf")
            time.sleep(sub.DELAY)
            try:
                sub.download_pdf(client, pdf_url, pdf_path)
                text = pdf.extract_text(pdf_path)
            except Exception as e:
                print(f"[sub fetch] {case_id}: pdf failed: {e}",
                      file=sys.stderr)
                continue  # stays 'new', retried next cycle
            if len(text) < _NARRATIVE_FLOOR:
                tier = "scanned"  # no usable text layer → summary fallback later
        else:
            tier = "summary-only"  # no PDF published; rely on HTML summary

        registration = sub.extract_registration(text)
        try:
            conn.execute(
                "UPDATE sub_reports SET narrative_text=?, source_tier=?, "
                "registration=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (text, tier, registration, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[sub fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows into sub_accidents. narrative = PDF text if
    >= floor, else the HTML summary if >= floor, else skip."""
    rows = conn.execute(
        "SELECT case_id, page_url, pdf_url, report_kind, aircraft, "
        "registration, location, date_of_occurrence, narrative_text, "
        "summary_text FROM sub_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        pdf_text = row["narrative_text"] or ""
        summary = row["summary_text"] or ""
        if len(pdf_text) >= _NARRATIVE_FLOOR:
            narrative = pdf_text
        elif len(summary) >= _NARRATIVE_FLOOR:
            narrative = summary  # PDF scanned/short → HTML-summary fallback
        else:
            conn.execute(
                "UPDATE sub_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO sub_accidents "
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
                "AT",
                "de",
                narrative,
                None,
                row["page_url"] or sub.BASE,
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE sub_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
