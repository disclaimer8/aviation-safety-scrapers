# aaiasb_ingest/harsia_pipeline.py
"""discover-harsia -> fetch-harsia -> parse-harsia -> build-harsia pipeline.

Second discovery path for aaiasb source: post-2023 reports from HARSIA
(www.harsia.gr), the successor to the abolished AAIASB authority.

Uses the same aaiasb_reports work table and aaiasb_accidents output table.
DEDUP: skips any case_id already present in aaiasb_accidents.
source_url points at harsia.gr post URL.
"""
import os
import sys
import time

from . import db, harsia
from .pdf import extract_text, MIN_NARRATIVE
from .text import make_site_slug

_NARRATIVE_FLOOR = 80


def discover_harsia(conn):
    """Walk harsia.gr listing pages (up to MAX_PAGES), visit each post for
    PDF links, INSERT new case_ids into aaiasb_reports. Idempotent."""
    inserted = 0
    with harsia.HarsiaBrowser() as browser:
        for page_num in range(1, harsia.MAX_PAGES + 1):
            try:
                html = browser.get_listing_html(page_num)
            except Exception as exc:
                print(f"[harsia discover] listing page {page_num}: {exc}", file=sys.stderr)
                # stop paging when we get no more posts
                break

            post_urls = harsia.parse_listing_posts(html)
            if not post_urls:
                # no posts on this page => we've gone past the last page
                break

            print(f"[harsia discover] page {page_num}: {len(post_urls)} posts")

            for post_url in post_urls:
                try:
                    post_html = browser.get_post_html(post_url)
                except Exception as exc:
                    print(f"[harsia discover] post {post_url}: {exc}", file=sys.stderr)
                    continue

                meta = harsia.parse_post(post_html, post_url)
                case_id = meta["case_id"]
                if not case_id:
                    print(f"[harsia discover] no case_id for {post_url}", file=sys.stderr)
                    continue

                # Skip council meeting minutes (e.g. "74/2024 ΣΥΝΕΔΡΙΑΣΗ ΣΥΜΒΟΥΛΙΟΥ")
                # Numeric case_ids >= 50 are typically administrative, not accident reports
                import re as _re
                _num_m = _re.match(r"^(\d+)-(\d{4})$", case_id)
                if _num_m and int(_num_m.group(1)) >= 50:
                    print(f"[harsia discover] {case_id}: skip (administrative/council, num>={_num_m.group(1)})")
                    continue

                # DEDUP: skip if already in aaiasb_accidents (from aaiasb.eu)
                already_built = conn.execute(
                    "SELECT 1 FROM aaiasb_accidents WHERE case_id=?", (case_id,)
                ).fetchone()
                if already_built:
                    print(f"[harsia discover] {case_id}: already in aaiasb_accidents (dedup)")
                    continue

                already_report = conn.execute(
                    "SELECT 1 FROM aaiasb_reports WHERE case_id=?", (case_id,)
                ).fetchone()
                if already_report:
                    # Already queued (idempotent)
                    continue

                conn.execute(
                    "INSERT INTO aaiasb_reports "
                    "(case_id, report_url, pdf_url, pdf_url_en, pdf_url_el, lang, "
                    " report_type, aircraft, registration, operator, "
                    " date_of_occurrence, location, source_tier, status, "
                    " discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        meta["report_url"],
                        meta["pdf_url"],
                        meta["pdf_url_en"],
                        meta["pdf_url_el"],
                        meta["lang"],
                        meta["report_type"],
                        None,  # aircraft: filled after parse
                        None,  # registration
                        None,  # operator
                        None,  # date_of_occurrence
                        None,  # location
                        "harsia",  # mark source_tier so we know the origin
                        db.STATUS_NEW,
                        db.now_ms(),
                        db.now_ms(),
                    ),
                )
                conn.commit()
                inserted += 1
                print(f"[harsia discover] inserted {case_id} (lang={meta['lang']}) pdfs={meta['pdf_urls']}")

    return inserted


def fetch_harsia(conn, pdf_dir):
    """Download PDFs for harsia rows with status='new'. Uses in-browser fetch
    to pass CF protection on the PDF endpoint."""
    os.makedirs(pdf_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_en, pdf_url_el, report_url "
        "FROM aaiasb_reports WHERE status=? AND source_tier='harsia'",
        (db.STATUS_NEW,),
    ).fetchall()
    if not rows:
        return 0

    processed = 0
    with harsia.HarsiaBrowser() as browser:
        # Warm browser on listing page so CF clearance is active
        try:
            browser.get_listing_html(1)
        except Exception as exc:
            print(f"[harsia fetch] warm-up failed: {exc}", file=sys.stderr)

        for row in rows:
            case_id = row["case_id"]
            # Priority: pdf_url (chosen), then en, then el
            candidates = []
            for u in (row["pdf_url"], row["pdf_url_en"], row["pdf_url_el"]):
                if u and u not in candidates:
                    candidates.append(u)

            pdf_path = None
            if candidates:
                safe = case_id.replace("/", "_").replace(" ", "_")
                dest = os.path.join(pdf_dir, "harsia-" + safe + ".pdf")
                last_exc = None
                for u in candidates:
                    try:
                        time.sleep(harsia.DELAY)
                        browser.download_pdf(u, dest)
                        pdf_path = dest
                        break
                    except Exception as exc:
                        last_exc = exc
                if pdf_path is None:
                    print(f"[harsia fetch] {case_id}: {last_exc}", file=sys.stderr)
                    continue  # leave as 'new' for retry

            try:
                conn.execute(
                    "UPDATE aaiasb_reports SET pdf_path=?, status=?, updated_at=? "
                    "WHERE case_id=?",
                    (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
                )
                conn.commit()
                processed += 1
            except Exception as exc:
                print(f"[harsia fetch] {case_id}: db {exc}", file=sys.stderr)

    return processed


def parse_harsia(conn):
    """pdftotext extraction for fetched harsia rows -> 'parsed'."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaiasb_reports "
        "WHERE status=? AND source_tier='harsia'",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            tier = "harsia"
        elif len(full_text) >= _NARRATIVE_FLOOR:
            tier = "harsia"
        elif pdf_path:
            tier = "harsia-scanned"
        else:
            tier = "harsia-none"

        conn.execute(
            "UPDATE aaiasb_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (full_text, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build_harsia(conn):
    """Emit aaiasb_accidents for parsed harsia rows with >= 80 chars.
    Skips rows that were already built (dedup). Everything else -> 'skipped'."""
    rows = conn.execute(
        "SELECT case_id, report_type, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, report_url, lang "
        "FROM aaiasb_reports "
        "WHERE status=? AND source_tier LIKE 'harsia%'",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"] or ""

        if tier == "harsia-scanned" or tier == "harsia-none":
            print(f"[harsia build] {row['case_id']}: skipped ({tier}, text={len(narrative)})")
            conn.execute(
                "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        if len(narrative) < _NARRATIVE_FLOOR:
            print(f"[harsia build] {row['case_id']}: skipped (text too short: {len(narrative)})")
            conn.execute(
                "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # Final dedup guard (race condition / re-run safety)
        already = conn.execute(
            "SELECT 1 FROM aaiasb_accidents WHERE case_id=?", (row["case_id"],)
        ).fetchone()
        if already:
            conn.execute(
                "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO aaiasb_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "GR",
                narrative,
                None,
                row["report_url"],
                row["report_type"],
                site_slug,
                row["lang"],
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
        print(f"[harsia build] built {row['case_id']} (lang={row['lang']}, text={len(narrative)})")

    return built
