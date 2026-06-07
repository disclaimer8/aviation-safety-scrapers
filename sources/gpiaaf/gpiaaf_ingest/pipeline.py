# gpiaaf_ingest/pipeline.py
"""
discover → fetch → build pipeline for GPIAAF Portugal (civil aviation).

⚠️ FULL-BROWSER source. discover() renders the Nuxt SPA listing root + each
year page and parses the report table (no PDFs yet). fetch() RE-USES a browser
session to follow each report's ``?v=`` route → capture the presigned-S3 PDF
(60-second expiry) via the browser download event → pdftotext. build() promotes
qualifying rows into ``gpiaaf_accidents`` (country PT).

  discover(conn, browser):        render listings + year tables → insert rows
  fetch(conn, browser, pdf_dir):  capture+save PDFs, pdftotext → narrative
  build(conn):                    promote into gpiaaf_accidents (floor 300)

Rows whose only Documento is a quarterly bulletin are kept as metadata with
status ``no_report`` and never fetched.
"""
import os
import sys
import time

from . import gpiaaf, db, pdf
from .text import make_site_slug

# build() floor: a row needs narrative >= this to become an accident.
_NARRATIVE_FLOOR = 300


def _base_case_id(source_url, row):
    """The STABLE (pre-collision) base case_id for a parsed row.

    Report rows key on the process number / d-number; no-report (bulletin-only)
    rows have no case number and no document, so they key deterministically on
    the year page + occurrence date + registration so re-runs are idempotent.
    """
    case_id = row.get("case_id")
    if case_id:
        return case_id
    if row.get("doc_url") or row.get("pdf_id"):
        return gpiaaf.fallback_case_id(row.get("doc_url"), row.get("pdf_id"))
    ident = "|".join(str(x or "") for x in (
        source_url, row.get("event_date"), row.get("registration"),
        row.get("aircraft"),
    ))
    return gpiaaf.fallback_case_id(ident)


def _row_identity(source_url, row):
    """A content-identity string for a row, independent of its case_id. Two
    genuinely-different rows that happen to share a case_id differ here; the
    SAME row re-discovered on a later run matches → idempotent."""
    return "|".join(str(x or "") for x in (
        source_url, row.get("event_date"), row.get("doc_url"),
        row.get("registration"), row.get("aircraft"), row.get("location"),
    ))


def _insert_row(conn, source_url, row, taken, seen_identity):
    """INSERT one parsed report row into gpiaaf_reports. Returns 1 if inserted.

    Idempotency keys on the row's content identity (independent of case_id), so
    a re-run inserts nothing. A collision suffix is applied only when two
    DISTINCT rows resolve to the same base case_id. Bulletin-only rows land as
    no_report.
    """
    identity = _row_identity(source_url, row)
    if identity in seen_identity:
        return 0
    seen_identity.add(identity)

    base = _base_case_id(source_url, row)
    case_id = base
    n = 2
    while case_id in taken or conn.execute(
        "SELECT 1 FROM gpiaaf_reports WHERE case_id=?", (case_id,)
    ).fetchone():
        case_id = f"{base}-{n}"
        n += 1
    taken.add(case_id)

    status = db.STATUS_NEW if row.get("has_report") else db.STATUS_NO_REPORT
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO gpiaaf_reports "
        "(case_id, doc_url, pdf_id, year, source_url, classification, "
        " aircraft, registration, location, event_date, lang, status, "
        " discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            row.get("doc_url"),
            row.get("pdf_id"),
            row.get("year"),
            source_url,
            row.get("classification"),
            row.get("aircraft"),
            row.get("registration"),
            row.get("location"),
            row.get("event_date"),
            "pt",
            status,
            ts,
            ts,
        ),
    )
    return 1


def discover(conn, browser, full=False, max_years=None, max_rows=None):
    """Render every populated aviation year page and INSERT its report rows.
    Returns count of newly inserted rows.

    max_years / max_rows cap the work for smoke runs.
    """
    taken = {
        r["case_id"]
        for r in conn.execute("SELECT case_id FROM gpiaaf_reports")
    }
    # seed seen content-identities from prior runs for cross-run idempotency
    seen_identity = {
        _row_identity(r["source_url"], {
            "event_date": r["event_date"], "doc_url": r["doc_url"],
            "registration": r["registration"], "aircraft": r["aircraft"],
            "location": r["location"],
        })
        for r in conn.execute(
            "SELECT source_url, event_date, doc_url, registration, aircraft, "
            "location FROM gpiaaf_reports"
        )
    }

    try:
        year_urls = browser.harvest_year_urls()
    except Exception as exc:
        print(f"[gpiaaf discover] root harvest failed: {exc}", file=sys.stderr)
        return 0
    if max_years:
        year_urls = year_urls[:max_years]

    inserted = 0
    for year_url in year_urls:
        year = gpiaaf.year_from_url(year_url)
        time.sleep(gpiaaf.DELAY)
        try:
            rows = browser.get_year_rows(year_url, year)
        except Exception as exc:
            print(f"[gpiaaf discover] year {year_url}: {exc}", file=sys.stderr)
            continue
        if max_rows is not None:
            rows = rows[:max_rows]
        for row in rows:
            try:
                inserted += _insert_row(conn, year_url, row, taken,
                                        seen_identity)
                conn.commit()
            except Exception as exc:
                print(f"[gpiaaf discover] insert {year_url}: {exc}",
                      file=sys.stderr)
    return inserted


def fetch(conn, browser, pdf_dir="pdfs", max_pdfs=None):
    """Capture each NEW (report-bearing) row's presigned-S3 PDF via the browser
    and pdftotext → narrative. Returns number of rows processed.

    no_report rows are never selected here.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, doc_url, pdf_id FROM gpiaaf_reports "
        "WHERE status=? AND doc_url IS NOT NULL",
        (db.STATUS_NEW,),
    ).fetchall()
    if max_pdfs is not None:
        rows = rows[:max_pdfs]

    for row in rows:
        case_id = row["case_id"]
        doc_url = row["doc_url"]
        pdf_id = row["pdf_id"]
        narrative = ""
        tier = "none"
        pdf_path = None
        s3_url = None

        safe = (pdf_id or case_id).replace("/", "_").replace(" ", "_")
        pdf_path = os.path.join(pdf_dir, f"{safe}.pdf")
        time.sleep(gpiaaf.DELAY)
        try:
            s3_url, captured_id = browser.capture_pdf(doc_url, pdf_path)
            pdf_id = pdf_id or captured_id
            # prefer the stable d-number as the on-disk filename once known
            if pdf_id and pdf_id != safe:
                stable_path = os.path.join(pdf_dir, f"{pdf_id}.pdf")
                try:
                    os.replace(pdf_path, stable_path)
                    pdf_path = stable_path
                except OSError:
                    pass
            text = pdf.extract_text(pdf_path)
            if len(text) >= pdf.MIN_NARRATIVE:
                narrative, tier = text, "pdf"
            else:
                narrative, tier = text, "scanned"
        except Exception as exc:
            print(f"[gpiaaf fetch] {case_id}: pdf {exc}", file=sys.stderr)
            pdf_path = None
            narrative, tier = "", "none"

        reg = gpiaaf.extract_registration(narrative)
        try:
            conn.execute(
                "UPDATE gpiaaf_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, pdf_url=?, pdf_id=COALESCE(pdf_id, ?), "
                "registration=COALESCE(registration, ?), status=?, updated_at=? "
                "WHERE case_id=?",
                (narrative, tier, pdf_path, s3_url, pdf_id, reg,
                 db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[gpiaaf fetch] {case_id}: db {exc}", file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote parsed rows with narrative >= floor into gpiaaf_accidents."""
    rows = conn.execute(
        "SELECT case_id, source_url, aircraft, classification, registration, "
        "location, event_date, narrative_text, source_tier "
        "FROM gpiaaf_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE gpiaaf_reports SET status=?, updated_at=? "
                "WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO gpiaaf_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["event_date"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "PT",
                narrative,
                None,
                row["source_url"] or gpiaaf.BASE,
                row["classification"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE gpiaaf_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
