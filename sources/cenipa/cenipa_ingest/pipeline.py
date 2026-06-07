# cenipa_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for CENIPA (Brazil).

Transport: CenipaBrowser (Playwright-based).  parse() and build() are
browser-free and operate only on the SQLite database.
"""
import os
import sys
import time

from . import cenipa, db
from .pdf import extract_text, MIN_NARRATIVE
from .text import make_site_slug

_NARRATIVE_FLOOR = 80  # chars; rows below this are treated as non-report events


def discover(conn, browser, full=False, max_pages=None):
    """Walk the CENIPA listing and INSERT new case_ids into cenipa_reports.

    Args:
        conn:       sqlite3 connection (row_factory=sqlite3.Row)
        browser:    CenipaBrowser instance (already started / used as ctx-mgr)
        full:       if True, ignore stop-on-empty heuristic (walk all pages)
        max_pages:  override the last_page() value (useful for smoke runs)

    Returns:
        Number of newly inserted rows.
    """
    # Page 1: fetch HTML + determine last page
    html = browser.get_listing_html(1)
    lp = max_pages or cenipa.last_page(html)

    inserted = 0
    for n in range(1, lp + 1):
        try:
            if n > 1:
                try:
                    html = browser.get_listing_html(n)
                    time.sleep(cenipa.DELAY)
                except Exception as exc:
                    print(f"[cenipa discover] page {n}: fetch error {exc}", file=sys.stderr)
                    continue

            rows = cenipa.parse_listing(html)
            if not rows:
                # Past the last real page — stop early
                break

            for row in rows:
                try:
                    case_id = row["case_id"]
                    # Skip if already known
                    exists = conn.execute(
                        "SELECT 1 FROM cenipa_reports WHERE case_id=?", (case_id,)
                    ).fetchone()
                    if exists:
                        continue

                    pdf_url, lang = cenipa.make_pdf_choice(row)

                    conn.execute(
                        "INSERT INTO cenipa_reports "
                        "(case_id, pdf_url, pdf_url_pt, pdf_url_en, lang, "
                        " classificacao, occurrence_type, aircraft, registration, "
                        " date_of_occurrence, location, status, discovered_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            case_id,
                            pdf_url,
                            row.get("pdf_url_pt"),
                            row.get("pdf_url_en"),
                            lang,
                            row.get("classificacao"),
                            row.get("occurrence_type"),
                            row.get("aircraft"),
                            row.get("registration"),
                            row.get("date_of_occurrence"),
                            row.get("location"),
                            db.STATUS_NEW,
                            db.now_ms(),
                            db.now_ms(),
                        ),
                    )
                    conn.commit()
                    inserted += 1
                except Exception as exc:
                    print(
                        f"[cenipa discover] page {n} row {row.get('case_id', '?')}: {exc}",
                        file=sys.stderr,
                    )

        except Exception as exc:
            print(f"[cenipa discover] page {n}: unexpected {exc}", file=sys.stderr)

    return inserted


def fetch(conn, browser, pdf_dir):
    """Download PDFs for status='new' rows and advance them to 'fetched'.

    Args:
        conn:     sqlite3 connection
        browser:  CenipaBrowser instance
        pdf_dir:  directory where PDFs are saved

    Returns:
        Number of rows processed (including failures that stay 'new').
    """
    os.makedirs(pdf_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_pt, pdf_url_en FROM cenipa_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    # Warm-up: PDFs are downloaded via an in-page fetch() (the only way past
    # Cloudflare). That requires the browser page to already be ON the CENIPA
    # origin and CF-cleared — otherwise the fetch is cross-origin/uncleared and
    # 403s. Navigating to the listing once establishes both.
    if rows:
        try:
            browser.get_listing_html(1)
        except Exception as exc:  # noqa: BLE001 — warm-up best-effort
            print(f"[cenipa fetch] warm-up navigation failed: {exc}", file=sys.stderr)

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        # Try the chosen url (EN-preferred) first; on failure fall back to the
        # other language variant (some EN PDFs genuinely 403 while the PT exists).
        candidates = []
        for u in (pdf_url, row["pdf_url_en"], row["pdf_url_pt"]):
            if u and u not in candidates:
                candidates.append(u)
        if candidates:
            safe_case_id = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe_case_id + ".pdf")
            last_exc = None
            for u in candidates:
                try:
                    time.sleep(cenipa.DELAY)
                    browser.download_pdf(u, dest)
                    pdf_path = dest
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            if pdf_path is None:
                print(f"[cenipa fetch] {case_id}: download {last_exc}", file=sys.stderr)
                continue  # leave status='new' so next run retries

        try:
            conn.execute(
                "UPDATE cenipa_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[cenipa fetch] {case_id}: db update {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract narrative text from downloaded PDFs and advance to 'parsed'.

    Returns:
        Number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM cenipa_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        if pdf_path:
            full_text = extract_text(pdf_path)
        else:
            full_text = ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            tier = "pdf"
        elif pdf_path:
            narrative = full_text
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE cenipa_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit cenipa_accidents records for high-quality parsed rows.

    A row qualifies when source_tier='pdf' AND len(narrative_text) >= _NARRATIVE_FLOOR.
    Everything else is marked 'skipped'.

    Returns:
        Number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, classificacao, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM cenipa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        source_tier = row["source_tier"] or ""

        if source_tier != "pdf" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE cenipa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO cenipa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "BR",
                narrative,
                None,
                source_url,
                row["classificacao"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE cenipa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
