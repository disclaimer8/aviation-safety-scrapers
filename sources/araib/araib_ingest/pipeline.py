# araib_ingest/pipeline.py
"""
discover → fetch(+detail+parse) → build pipeline for ARAIB South Korea.

ARAIB is a 3-stage source (listing → DTL detail page → PDF). To keep the
standard discover/fetch/build/all CLI contract the DTL stage is folded into
fetch:

discover() walks the paginated server-rendered listing (10 rows/page,
`&lcmspage=N`). ⚠️ The paginator widget only renders a fixed window of links,
so we DON'T trust it — we keep requesting pages until a page yields no NEW idx,
then stop. Each row is INSERTed keyed on its stable numeric `idx` (the canonical
ARAIB case number is not known until the PDF is read). 55 English aviation
reports total.

fetch() for each status='new' row: GET the DTL page (full title + the DWN.jsp
PDF link, scraped never constructed), download the PDF, pdftotext, then pull the
case number / occurrence date / registration / operator / aircraft / location
from the in-PDF synopsis. case_id is the normalised case number
('aar2404'/'air1906'), falling back to 'araib-{idx}'. A PDF with no usable text
layer is tiered 'scanned'. The WebtoB TMOSH 307-cookie handshake and cold-TLS
resets are handled in araib.fetch_page / download_pdf (persistent client cookie
jar + retry/backoff); tiny 624-byte wrong-node stubs are treated as failures.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
araib_accidents (country KR, lang en).
"""
import os
import sys
import time

from . import araib, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300
_MAX_PAGES = 50  # hard safety stop for the page walk (55 reports = ~6 pages)


def discover(conn, client, full=False):
    """
    Walk the paginated listing until a page yields no NEW idx. INSERT new rows
    keyed on idx. Returns count inserted.
    """
    taken = {
        r["idx"]
        for r in conn.execute(
            "SELECT idx FROM araib_reports WHERE idx IS NOT NULL"
        )
    }
    inserted = 0
    page = 1
    while page <= _MAX_PAGES:
        time.sleep(araib.DELAY)
        page_url = araib.listing_page_url(page)
        try:
            page_html = araib.fetch_page(client, page_url)
        except Exception as e:  # noqa: BLE001
            print(f"[araib discover] page {page}: failed: {e}",
                  file=sys.stderr)
            break

        rows = araib.parse_listing(page_html)
        # ⚠️ Stop when the page has no idx the catalogue hasn't already shown
        # (the paginator widget can't be trusted to advertise the last page).
        new_on_page = [r for r in rows if r["idx"] not in taken]
        if not new_on_page:
            break

        for rec in new_on_page:
            taken.add(rec["idx"])
            if conn.execute(
                "SELECT 1 FROM araib_reports WHERE idx=?", (rec["idx"],)
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO araib_reports "
                "(idx, dtl_url, title, publish_date, view_count, "
                "status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec["idx"],
                    rec["dtl_url"],
                    rec["title"],
                    rec["publish_date"],
                    rec["view_count"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
        page += 1
    return inserted


def _assign_case_id(conn, idx, case_number):
    """case_id = case number (canonical) or 'araib-{idx}'; collision-suffixed."""
    base = araib.case_id_from(idx, case_number)
    taken = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM araib_reports "
            "WHERE case_id IS NOT NULL AND idx<>?",
            (idx,),
        )
    }
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: GET the DTL page → PDF url, download the PDF,
    pdftotext, extract the synopsis fields, assign case_id. Returns row count.
    """
    rows = conn.execute(
        "SELECT idx, dtl_url, title FROM araib_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        idx = row["idx"]
        time.sleep(araib.DELAY)
        # ── stage 1: DTL page → full title + PDF url ──
        try:
            dtl_html = araib.fetch_page(client, row["dtl_url"])
        except Exception as e:  # noqa: BLE001
            print(f"[araib fetch] {idx}: dtl failed: {e}", file=sys.stderr)
            continue  # stays 'new'
        detail = araib.parse_dtl(dtl_html)
        pdf_url = detail["pdf_url"]
        full_title = detail["title"] or row["title"]
        if not pdf_url:
            print(f"[araib fetch] {idx}: no PDF link on DTL", file=sys.stderr)
            continue  # stays 'new'; retried next cycle

        # ── stage 2: download PDF + text ──
        pdf_path = os.path.join(pdf_dir, f"{idx}.pdf")
        text = ""
        tier = "pdf"
        time.sleep(araib.DELAY)
        try:
            araib.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:  # noqa: BLE001
            print(f"[araib fetch] {idx}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new'
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"

        # ── stage 3: synopsis extraction ──
        case_number = araib.extract_case_number(text)
        case_id = _assign_case_id(conn, idx, case_number)
        # Registration: title first (often carries HL-xxxx), then PDF text.
        registration = (
            araib.extract_registration(full_title)
            or araib.extract_registration(text)
        )
        # event_date from synopsis; fall back to publish date on parse failure.
        event_date = araib.extract_event_date(text)
        operator = araib.extract_operator(text)
        aircraft = araib.extract_aircraft(text)
        location = araib.extract_location(text)

        try:
            conn.execute(
                "UPDATE araib_reports SET case_id=?, pdf_url=?, title=?, "
                "case_number=?, registration=?, event_date=?, operator=?, "
                "aircraft=?, location=?, narrative_text=?, source_tier=?, "
                "pdf_path=?, status=?, updated_at=? WHERE idx=?",
                (
                    case_id, pdf_url, full_title, case_number, registration,
                    event_date, operator, aircraft, location, text, tier,
                    pdf_path, db.STATUS_PARSED, db.now_ms(), idx,
                ),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[araib fetch] {idx}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into araib_accidents."""
    rows = conn.execute(
        "SELECT idx, case_id, dtl_url, title, registration, event_date, "
        "publish_date, operator, aircraft, location, narrative_text "
        "FROM araib_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE araib_reports SET status=?, updated_at=? WHERE idx=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["idx"]),
            )
            conn.commit()
            continue

        # event_date: synopsis occurrence date, else fall back to publish date.
        event_date = row["event_date"] or row["publish_date"]
        report_type = araib.report_type_from(row["title"], narrative)
        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO araib_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, probable_cause, source_url, "
            "report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                event_date,
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "KR",
                "en",
                narrative,
                None,
                row["dtl_url"] or araib.BASE,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE araib_reports SET status=?, updated_at=? WHERE idx=?",
            (db.STATUS_BUILT, db.now_ms(), row["idx"]),
        )
        conn.commit()
        built += 1
    return built
