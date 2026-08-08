# rosap_ingest/pipeline.py
"""discover → fetch → parse → build for the ROSA P CAB collection.

discover(): walk the listing with a real browser, pairing each item link with
  its own anchor text, and INSERT new pids. A page that serves bytes but
  yields no anchors raises rather than being read as the end of the listing.

fetch(): download each PDF through Chrome's own download path — Akamai
  refuses every other client. A failure keeps the row at 'new' so the next
  run retries it.

parse(): OCR. These are image-only scans without exception, so there is no
  text layer to try first.

build(): emit rosap_accidents for reports only. Supplements ([Amendment],
  [Hearing Notice], …) are marked 'skipped': they are real documents but not
  separate accidents.
"""
import os
import sys
import time

from . import db, rosap
from .pdf import ocr_extract
from .text import make_site_slug

_NARRATIVE_FLOOR = 300  # OCR that recovers less than this is not a narrative


def _browser_page(playwright, downloads_ok=False):
    browser = playwright.chromium.launch(headless=False, args=["--no-sandbox"])
    ctx = browser.new_context(user_agent=rosap.UA, locale="en-US",
                              accept_downloads=downloads_ok)
    return browser, ctx.new_page()


def discover(conn, page, max_pages=None):
    """Walk the collection listing; INSERT new pids. Returns inserted count."""
    inserted = 0
    offset = 0
    pages_walked = 0
    while True:
        if max_pages is not None and pages_walked >= max_pages:
            break
        url = rosap.listing_url(offset)
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1800)

        pairs = page.eval_on_selector_all(rosap.ANCHOR_SELECTOR, rosap.ANCHOR_JS)
        rows = rosap.parse_listing(pairs)
        if not rows:
            if offset == 0:
                # The collection has 791 items and is not going to empty. Zero
                # anchors means the markup moved, which a stop-on-empty walk
                # would otherwise report as a clean, complete run.
                raise RuntimeError(
                    f"[rosap discover] {url} yielded 0 item anchors — "
                    "listing markup has changed")
            break

        for r in rows:
            if conn.execute("SELECT 1 FROM rosap_reports WHERE pid=?",
                            (r["pid"],)).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO rosap_reports (pid, title, operator, location, "
                "date_of_occurrence, doc_kind, pdf_url, status, discovered_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["pid"], r["title"], r["operator"], r["location"],
                 r["event_date"], r["doc_kind"], r["pdf_url"], db.STATUS_NEW,
                 ts, ts),
            )
            inserted += 1
        conn.commit()

        pages_walked += 1
        if len(rows) < rosap.PAGE_SIZE:
            break
        offset += rosap.PAGE_SIZE
        time.sleep(rosap.DELAY)

    return inserted


def fetch(conn, page, pdf_dir="pdfs"):
    """Download each 'new' row's PDF through the browser. Returns rows seen."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT pid, pdf_url FROM rosap_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        pid = row["pid"]
        dest = os.path.join(pdf_dir, f"rosap-{pid}.pdf")
        try:
            with page.expect_download(timeout=120000) as dl:
                try:
                    page.goto(row["pdf_url"], timeout=120000)
                except Exception:
                    pass  # Chrome aborts the navigation to start the download
            dl.value.save_as(dest)
            with open(dest, "rb") as fh:
                if fh.read(5) != b"%PDF-":
                    raise RuntimeError("response was not a PDF")
        except Exception as exc:
            # Stay 'new'. A download we could not complete is a transient
            # fault, not a report without content, and only 'new' is retried.
            print(f"[rosap fetch] {pid}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue

        conn.execute(
            "UPDATE rosap_reports SET pdf_path=?, pdf_bytes=?, status=?, "
            "updated_at=? WHERE pid=?",
            (dest, os.path.getsize(dest), db.STATUS_FETCHED, db.now_ms(), pid),
        )
        conn.commit()
        time.sleep(rosap.DELAY)

    return len(rows)


def parse(conn, enable_ocr=True):
    """OCR each fetched PDF. Returns rows processed.

    No pdftotext first pass: every PDF in this collection is an image-only
    scan, verified across three decades, so trying the text layer would only
    ever return an empty string.
    """
    rows = conn.execute(
        "SELECT pid, pdf_path FROM rosap_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        text = ""
        if enable_ocr and row["pdf_path"] and os.path.exists(row["pdf_path"]):
            text = ocr_extract(row["pdf_path"], lang=rosap.OCR_LANG)
        tier = "ocr" if len(text) >= _NARRATIVE_FLOOR else "scanned"
        conn.execute(
            "UPDATE rosap_reports SET narrative_text=?, source_tier=?, "
            "status=?, updated_at=? WHERE pid=?",
            (text, tier, db.STATUS_PARSED, db.now_ms(), row["pid"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Project 'parsed' reports into rosap_accidents. Returns rows built."""
    rows = conn.execute(
        "SELECT pid, title, operator, location, date_of_occurrence, doc_kind, "
        "narrative_text, pdf_url FROM rosap_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        supplement = (row["doc_kind"] or "report") != "report"
        if supplement or len(narrative) < _NARRATIVE_FLOOR:
            # A supplement is a real document but not a separate accident, and
            # an OCR pass that recovered nothing is not a narrative.
            conn.execute(
                "UPDATE rosap_reports SET status=?, updated_at=? WHERE pid=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["pid"]),
            )
            conn.commit()
            continue

        case_id = f"rosap-{row['pid']}"
        conn.execute(
            "INSERT OR REPLACE INTO rosap_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row["date_of_occurrence"],
                None,   # the type is in the OCR text; a later pass, not a guess
                None,   # registration feeds occurrence dedup — not from a regex
                row["operator"],
                row["location"],
                rosap.country_of(row["location"]),
                narrative,
                None,
                rosap.item_url(row["pid"]),
                "Final Report",
                make_site_slug(None, None, row["location"]),
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE rosap_reports SET status=?, updated_at=? WHERE pid=?",
            (db.STATUS_BUILT, db.now_ms(), row["pid"]),
        )
        conn.commit()
        built += 1

    return built
