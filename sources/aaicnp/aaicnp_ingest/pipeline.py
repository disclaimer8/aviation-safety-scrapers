# aaicnp_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for Nepal AAIC (CAAN).

discover(): walks the CAAN listing surfaces (static SMD pages + paginated
  all-news/all-notice with their detail posts) harvesting report-PDF hrefs to
  whatever host, plus a seed list of known gov/CDN final reports.  Each report
  URL is inserted into aaicnp_reports keyed by a PROVISIONAL case_id derived
  from the URL filename (intrinsic to the URL, order-independent).  Idempotent
  by pdf_url and by case_id.  Gov/CDN copies preferred.

fetch(): downloads each status='new' row's PDF and advances to 'fetched'.
  Per-row try/except keeps a failed row at 'new' for the next cycle.

parse(): extracts text with pdftotext.  Re-derives the INTRINSIC case_id from
  the report text (report reference, else registration+date) and rewrites the
  staging row key when it differs from the provisional URL-based id (dedupe by
  merging into an existing intrinsic id).  Scanned-aware: text below
  _SCANNED_FLOOR (~500 chars) -> tier 'scanned' and the row is left for build()
  to skip.  tier: 'pdf' (>=MIN_NARRATIVE), 'short', 'scanned', 'none'.

build(): emits aaicnp_accidents (country='NP').  Skips rows whose narrative is
  below _NARRATIVE_FLOOR or whose tier is 'scanned'/'none'.
"""
import os
import sys
import time

from . import aaicnp, db, text
from .pdf import extract_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300     # chars; rows below are non-report events
_SCANNED_FLOOR = 500      # chars; below this an image-only/scanned PDF is assumed


def _provisional_case_id(url):
    """Provisional intrinsic id from the URL filename (order-independent)."""
    fn = aaicnp._filename_of(url)
    return aaicnp.make_case_id(filename=fn)


def _collect_report_urls(conn, client):
    """Walk all listing surfaces + seed -> ordered de-duplicated [(url, text)]."""
    found = {}  # url -> anchor_text (first wins)

    def add(url, anchor=""):
        if url not in found:
            found[url] = anchor

    # 1. static SMD pages
    for page_url in aaicnp.STATIC_LISTING_URLS:
        try:
            time.sleep(aaicnp.DELAY)
            r = client.get(page_url)
            r.raise_for_status()
            html = r.content.decode("utf-8", "replace") if isinstance(r.content, bytes) else r.text
        except Exception as exc:
            print(f"[aaicnp discover] {page_url}: {exc}", file=sys.stderr)
            continue
        for url, anchor in aaicnp.harvest_report_links(html):
            add(url, anchor)

    # 2. paginated post listings -> detail posts -> harvest
    for listing in (aaicnp.ALL_NEWS_URL, aaicnp.ALL_NOTICE_URL):
        post_urls = []
        seen_pages = set()
        for page in range(1, aaicnp.MAX_LIST_PAGES + 1):
            page_url = f"{listing}?page={page}"
            try:
                time.sleep(aaicnp.DELAY)
                r = client.get(page_url)
                r.raise_for_status()
                html = r.content.decode("utf-8", "replace") if isinstance(r.content, bytes) else r.text
            except Exception as exc:
                print(f"[aaicnp discover] {page_url}: {exc}", file=sys.stderr)
                break
            posts = aaicnp.iter_post_links(html)
            sig = tuple(posts)
            if not posts or sig in seen_pages:
                break  # pagination exhausted / repeating
            seen_pages.add(sig)
            for p in posts:
                if p not in post_urls:
                    post_urls.append(p)
        # visit each detail post and harvest report links from its body
        for post_url in post_urls:
            try:
                time.sleep(aaicnp.DELAY)
                r = client.get(post_url)
                r.raise_for_status()
                html = r.content.decode("utf-8", "replace") if isinstance(r.content, bytes) else r.text
            except Exception as exc:
                print(f"[aaicnp discover] {post_url}: {exc}", file=sys.stderr)
                continue
            for url, anchor in aaicnp.harvest_report_links(html):
                add(url, anchor)

    # 3. seed list (gov/CDN copies guaranteed present)
    for url in aaicnp.SEED_REPORT_URLS:
        add(url, "")

    return list(found.items())


def discover(conn, client, full=False):
    """Harvest report URLs and INSERT new rows into aaicnp_reports.

    full: accepted for API parity (the full surface is always walked; per-row
          idempotency handles re-runs).

    Returns: number of rows inserted.
    """
    report_urls = _collect_report_urls(conn, client)

    inserted = 0
    for url, anchor in report_urls:
        # idempotent by pdf_url
        if conn.execute(
            "SELECT 1 FROM aaicnp_reports WHERE pdf_url=?", (url,)
        ).fetchone():
            continue
        case_id = _provisional_case_id(url)
        if not case_id:
            continue
        # idempotent by case_id (same report reached via a different surface)
        if conn.execute(
            "SELECT 1 FROM aaicnp_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaicnp_reports "
            "(case_id, report_url, pdf_url, title, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (case_id, url, url, anchor or None, "en", db.STATUS_NEW, ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download each status='new' row's PDF; advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaicnp_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe + ".pdf")
            try:
                time.sleep(aaicnp.DELAY)
                aaicnp.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaicnp fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay 'new'

        try:
            conn.execute(
                "UPDATE aaicnp_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaicnp fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def _rekey(conn, old_id, new_id):
    """Rewrite the staging row key old_id -> new_id (intrinsic id finalisation).

    If new_id already exists, the old row is dropped (dedupe) and False is
    returned so the caller skips further processing of old_id.
    """
    if new_id == old_id:
        return True
    if conn.execute(
        "SELECT 1 FROM aaicnp_reports WHERE case_id=?", (new_id,)
    ).fetchone():
        conn.execute("DELETE FROM aaicnp_reports WHERE case_id=?", (old_id,))
        conn.commit()
        return False
    conn.execute(
        "UPDATE aaicnp_reports SET case_id=? WHERE case_id=?", (new_id, old_id)
    )
    conn.commit()
    return True


def parse(conn):
    """Extract text, finalise intrinsic case_id, set narrative + tier.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, pdf_url FROM aaicnp_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    processed = 0
    for row in rows:
        case_id = row["case_id"]
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        # finalise INTRINSIC case_id from the report text
        meta = aaicnp.extract_metadata(
            full_text, filename=aaicnp._filename_of(row["pdf_url"] or "")
        )
        intrinsic = meta["case_id"] or case_id
        if not _rekey(conn, case_id, intrinsic):
            # merged into an existing intrinsic id — drop this duplicate
            continue
        case_id = intrinsic

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= _SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif full_text:
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE aaicnp_reports SET "
            "narrative_text=?, source_tier=?, registration=COALESCE(registration,?), "
            "date_of_occurrence=COALESCE(date_of_occurrence,?), "
            "event_class=COALESCE(event_class,?), aircraft=COALESCE(aircraft,?), "
            "title=COALESCE(?,title), status=?, updated_at=? "
            "WHERE case_id=?",
            (
                narrative, tier, meta["registration"], meta["event_date"],
                meta["event_class"], meta["aircraft"], meta["title"],
                db.STATUS_PARSED, db.now_ms(), case_id,
            ),
        )
        conn.commit()
        processed += 1

    return processed


def build(conn):
    """Emit aaicnp_accidents rows (country='NP') or skip.

    Skip (status -> 'skipped') when narrative < _NARRATIVE_FLOOR, tier is
    'scanned' or 'none'.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aaicnp_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        if len(narrative) < _NARRATIVE_FLOOR or tier in ("scanned", "none"):
            conn.execute(
                "UPDATE aaicnp_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO aaicnp_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"], row["date_of_occurrence"], row["aircraft"],
                row["registration"], row["operator"], row["location"], "NP",
                narrative, None, source_url, row["event_class"], site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaicnp_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
