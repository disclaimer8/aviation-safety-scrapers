# india_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for AAIB India.

discover() GETs the single index.html, skips preliminary/interim filenames,
inserts new rows keyed on pdf_url (rel_path) with a synthetic case_id
({year}_{VT-REG}, collision-suffixed) — AAIB India has no official numbering.

fetch() downloads each PDF, runs pdftotext, extracts best-effort metadata
from the title page (two era formats — see india.py), advances to 'parsed'.
Empty pdftotext output → source_tier='scanned' (kept for build() to skip).

build() promotes 'parsed' rows whose narrative >= _NARRATIVE_FLOOR into
india_accidents (country IN).
"""
import os
import re
import sys
import time

from . import db, india, pdf
from .text import make_site_slug

# Every value below is derived from the source's own HTML/JSON, so it must not
# be trusted as a path component: os.path.join with a value containing "/" or
# ".." writes outside pdf_dir. BFU already carried this guard; most packages
# did not.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    """Reduce an untrusted identifier to a single, safe path component."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(name or ""))
    cleaned = cleaned.lstrip(".") or "unnamed"
    return cleaned[:120]


_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """
    Parse the single index page; INSERT new (pdf_url-keyed) rows.
    Returns: number of rows inserted.
    """
    html = india.fetch_index(client)
    rows = india.parse_index(html)
    if not rows:
        # 214 rows from this source are already in production, so an
        # empty listing is the markup changing — not the authority
        # publishing nothing. Returning 0 here is indistinguishable
        # from a clean run, which is how a dead scraper stays quiet.
        raise RuntimeError(
            "[india discover] listing parsed to zero rows. The markup has"
            " probably changed; refusing to report an empty run as success."
        )

    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM india_reports WHERE case_id IS NOT NULL"
        )
    }
    inserted = 0
    for r in rows:
        if conn.execute(
            "SELECT 1 FROM india_reports WHERE pdf_url=?", (r["pdf_url"],)
        ).fetchone():
            continue
        case_id = india.make_case_id(
            r["year"], r["registration"], r["rel_path"], taken=taken
        )
        taken.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO india_reports "
            "(pdf_url, case_id, year, report_kind, registration, status, "
            "discovered_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                r["pdf_url"],
                case_id,
                r["year"],
                r["report_kind"],
                r["registration"],
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
    For each status='new' row: download the PDF, pdftotext, extract metadata,
    advance to 'parsed'.  Per-row try/except — a failing row stays 'new'.
    Returns: number of rows iterated.
    """
    rows = conn.execute(
        "SELECT pdf_url, case_id FROM india_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(india.DELAY)
        pdf_path = os.path.join(pdf_dir, _safe_filename(case_id) + ".pdf")
        try:
            india.download_pdf(client, row["pdf_url"], pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[india fetch] {case_id}: failed: {e}", file=sys.stderr)
            continue

        tier = "pdf" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        meta = india.parse_pdf_meta(text) if text else {
            "registration": None, "aircraft": None, "operator": None,
            "location": None, "event_date": None,
        }
        try:
            conn.execute(
                "UPDATE india_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, "
                "registration=COALESCE(registration, ?), "
                "aircraft=?, operator=?, location=?, date_of_occurrence=?, "
                "status=?, updated_at=? WHERE pdf_url=?",
                (
                    text,
                    tier,
                    pdf_path,
                    meta["registration"],
                    meta["aircraft"],
                    meta["operator"],
                    meta["location"],
                    meta["event_date"],
                    db.STATUS_PARSED,
                    db.now_ms(),
                    row["pdf_url"],
                ),
            )
            conn.commit()
        except Exception as e:
            print(f"[india fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """
    Promote 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
    india_accidents; shorter (incl. scans) → 'skipped'.
    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT pdf_url, case_id, report_kind, aircraft, registration, "
        "operator, location, date_of_occurrence, narrative_text "
        "FROM india_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE india_reports SET status=?, updated_at=? WHERE pdf_url=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pdf_url"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO india_accidents "
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
                "IN",
                narrative,
                None,
                row["pdf_url"],
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE india_reports SET status=?, updated_at=? WHERE pdf_url=?",
            (db.STATUS_BUILT, db.now_ms(), row["pdf_url"]),
        )
        conn.commit()
        built += 1
    return built
