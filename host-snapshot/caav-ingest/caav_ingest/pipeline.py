# caav_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for CAAV (Vietnam).

discover(): walks the listing page(s), then for each report row fetches the
  detail page to extract the report title, the CDN PDF url, and intrinsic
  metadata (registration / event date / aircraft).  Derives an
  order-independent case_id via caav.make_case_id and INSERTs new rows into
  caav_reports.  Idempotent — existing case_ids are skipped.  A detail-page
  failure is logged and skipped (row not inserted; retried next run).

fetch(): for each status='new' row with a pdf_url, downloads the CDN PDF and
  advances to 'fetched'.  Rows without a pdf_url advance to 'fetched' with
  pdf_path=None.  Per-row try/except: a download failure keeps the row at
  'new' for retry.

parse(): extracts text via pdftotext.  Scanned-aware — text shorter than
  ~MIN_NARRATIVE is tier 'short'; reports under _SCANNED_FLOOR chars are
  treated as scanned ('scanned' tier) and skipped at build time; no text at
  all is 'none'.

build(): emits caav_accidents rows; narratives below _NARRATIVE_FLOOR are
  skipped.  country='VN'.
"""
import os
import sys
import time

from . import caav, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; below this a row is not a usable report
_SCANNED_FLOOR = 500    # chars; below this a PDF is treated as scanned/image


def _fetch_detail_html(client, detail_url):
    """GET a detail page → decoded HTML, or None on error (logged)."""
    try:
        d_resp = client.get(detail_url)
        d_resp.raise_for_status()
        return (
            d_resp.content.decode("utf-8", "replace")
            if isinstance(d_resp.content, bytes)
            else d_resp.text
        )
    except Exception as exc:
        print(f"[caav discover] detail {detail_url}: {exc}", file=sys.stderr)
        return None


def _insert_report(conn, detail_url, meta, pub_date=None, listing_title=None):
    """INSERT a discovered report row if its case_id is new.  Returns 1/0."""
    title = meta.get("title") or listing_title
    registration = meta.get("registration")
    date_iso = meta.get("date_iso") or pub_date
    aircraft = meta.get("aircraft")
    pdf_url = meta.get("pdf_url")

    case_id = caav.make_case_id(registration, meta.get("date_iso"), pdf_url)
    if not case_id:
        print(f"[caav discover] no case_id for {detail_url}", file=sys.stderr)
        return 0
    if conn.execute(
        "SELECT 1 FROM caav_reports WHERE case_id=?", (case_id,)
    ).fetchone():
        return 0  # already known

    ts = db.now_ms()
    conn.execute(
        "INSERT INTO caav_reports "
        "(case_id, report_url, pdf_url, title, event_class, aircraft, "
        "registration, date_of_occurrence, lang, status, "
        "discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id,
            detail_url,
            pdf_url,
            title,
            caav.event_class_from_title(title),
            aircraft,
            registration,
            date_iso,
            "en",
            db.STATUS_NEW,
            ts,
            ts,
        ),
    )
    conn.commit()
    return 1


def discover(conn, client, full=False):
    """Discover report case_ids and INSERT new ones.  Returns count inserted.

    Two complementary paths:
      A. parse the server-rendered page-1 listing (the SEED rows).
      B. ID-enumeration over the detail-page id space.  The pager pages 2-4 are
         JS-empty shells, but direct /doc-detail/<slug>-<id>.htm enumeration
         reaches ALL reports over plain httpx.  A bounded window is derived from
         the page-1 seed ids; rows are kept only when they are genuine reports
         (CDN PDF present AND report-type title, or titleless-with-PDF).  Empty
         shells (no PDF) are skipped, and the forward scan stops after a run of
         consecutive empty shells so new higher ids are picked up cheaply.
    """
    inserted = 0
    discovered_reports = 0   # genuine report detail pages SEEN (for the tripwire)
    visited_urls: set[str] = set()
    seed_ids: list[int] = []

    # ── Path A: page-1 listing seed ──────────────────────────────────────────
    for listing_url in caav.iter_listing_urls():
        try:
            resp = client.get(listing_url)
            resp.raise_for_status()
            html = (
                resp.content.decode("utf-8", "replace")
                if isinstance(resp.content, bytes)
                else resp.text
            )
        except Exception as exc:
            print(f"[caav discover] {listing_url}: {exc}", file=sys.stderr)
            continue

        for row in caav.parse_listing(html, listing_url):
            detail_url = row["detail_url"]
            if detail_url in visited_urls:
                continue
            visited_urls.add(detail_url)
            did = caav.detail_id_from_url(detail_url)
            if did is not None:
                seed_ids.append(did)

            time.sleep(caav.DELAY)
            d_html = _fetch_detail_html(client, detail_url)
            if d_html is None:
                continue
            meta = caav.parse_detail(d_html)
            if not caav.is_report_detail(meta):
                continue
            discovered_reports += 1
            inserted += _insert_report(
                conn, detail_url, meta,
                pub_date=row.get("pub_date"),
                listing_title=row.get("listing_title"),
            )

    # ── Path B: bounded ID-enumeration ───────────────────────────────────────
    window = caav.id_window_from_seeds(seed_ids)
    if window:
        anchor_fwd_start = max(seed_ids) + 1
        consecutive_empty = 0
        for doc_id in window:
            detail_url = caav.detail_url_for_id(doc_id)
            # the listing seed may use a different slug for the same id — dedup
            # by id, not by url
            if any(caav.detail_id_from_url(u) == doc_id for u in visited_urls):
                continue
            visited_urls.add(detail_url)

            time.sleep(caav.DELAY)
            d_html = _fetch_detail_html(client, detail_url)
            if d_html is None:
                continue
            meta = caav.parse_detail(d_html)

            is_report = caav.is_report_detail(meta)
            # forward-scan sentinel: count consecutive EMPTY shells (no PDF) past
            # the seed cluster; stop once we hit a long empty run.
            if doc_id >= anchor_fwd_start:
                if not meta.get("pdf_url"):
                    consecutive_empty += 1
                    if consecutive_empty >= caav.ID_FWD_EMPTY_STOP:
                        break
                else:
                    consecutive_empty = 0

            if not is_report:
                continue
            discovered_reports += 1
            inserted += _insert_report(conn, detail_url, meta)

    if discovered_reports == 0:
        print("[caav WARN] discovery yielded 0 report rows", file=sys.stderr)

    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows.  Returns rows iterated."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM caav_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe_case_id = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe_case_id + ".pdf")
            try:
                time.sleep(caav.DELAY)
                caav.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[caav fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new'

        try:
            conn.execute(
                "UPDATE caav_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[caav fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract PDF text; set source_tier.  Returns rows processed."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM caav_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier = "", "none"
        elif len(full_text) < _SCANNED_FLOOR:
            # too little text from a PDF that exists → almost certainly scanned
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        conn.execute(
            "UPDATE caav_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit caav_accidents rows.  Returns rows built (not skipped)."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM caav_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if row["source_tier"] == "scanned" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE caav_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO caav_accidents "
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
                "VN",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE caav_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
