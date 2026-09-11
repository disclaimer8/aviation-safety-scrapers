# baaid_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for Bahamas AAID (baaid.org).

discover(): GET /accidents, parse the inline PDF listing, INSERT one row per
  UNIQUE Wix PDF file-id into baaid_reports.  case_id is the intrinsic file-id
  (stable; no encounter-order suffix).  Idempotent — known case_ids skipped.

fetch(): download each status='new' report's PDF -> 'fetched'.  Per-row
  try/except keeps a failed download at 'new' for retry.

parse(): pdftotext each PDF.  Scanned-aware: text length <= SCANNED_CEILING
  (~500 chars) means an image-only scan -> tier 'scanned' (build skips it).
  Otherwise tier 'pdf' (>= MIN_NARRATIVE) or 'short'.  Authoritative metadata
  (OCC number, registration, aircraft, date, location) is re-extracted from the
  PDF text here, overriding the noisy listing hints.

build(): emit baaid_accidents rows (country 'BS').  Rows below _NARRATIVE_FLOOR
  chars are skipped.  report_type carries the OCC number when known.
"""
import os
import re
import sys
import time

from . import baaid, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_CEILING

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars


def discover(conn, client, full=False):
    resp = client.get(baaid.INDEX_URL)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content

    rows = baaid.parse_listing(html)
    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute("SELECT 1 FROM baaid_reports WHERE case_id=?", (case_id,)).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO baaid_reports "
            "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, title, "
            "event_class, aircraft, registration, date_of_occurrence, location, "
            "lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                baaid.INDEX_URL,
                row.get("pdf_url"),
                None,
                row.get("pdf_url"),  # English source
                row.get("title"),
                row.get("event_class"),
                None,
                row.get("registration"),
                row.get("date_of_occurrence"),
                None,
                "en",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM baaid_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchall()
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        pdf_path = None
        if pdf_url:
            dest = os.path.join(pdf_dir, case_id.replace("/", "_") + ".pdf")
            try:
                time.sleep(baaid.DELAY)
                baaid.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[baaid fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay 'new'
        try:
            conn.execute(
                "UPDATE baaid_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[baaid fetch] {case_id}: db {exc}", file=sys.stderr)
    return len(rows)


# ── PDF-text metadata extractors ──────────────────────────────────────────────

_REG_LINE_RE = re.compile(
    r"(?:Aircraft\s+)?Registration[:\s]+([A-Z][A-Z0-9\-]{2,9})\b", re.IGNORECASE
)
_AC_LINE_RE = re.compile(
    r"Aircraft\s+(?:Make\s*/?\s*Model|Type)[:\s]+([^\n:]{2,60})", re.IGNORECASE
)
_DATE_LINE_RE = re.compile(
    r"(?:Date of Occurrence|Occurrence Date(?:\s*&\s*Time)?|DATE)[^\n]*?"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)[,\s]+((?:19|20)\d{2})",
    re.IGNORECASE,
)
# Location only from a clean single-line "Location: <value>" header field; reject
# values that are actually the next label or sentence fragments.
_LOC_LINE_RE = re.compile(r"\bLocation[:\s]+([^\n:]{3,70})", re.IGNORECASE)
_LABEL_NOISE_RE = re.compile(
    r"registration|persons on board|occurrence|aircraft|status|summary|board",
    re.IGNORECASE,
)

_MONTHS = {
    m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], 1)
}


def _pdf_date_iso(t):
    m = _DATE_LINE_RE.search(t)
    if not m:
        return None
    day = int(m.group(1)); mon = _MONTHS.get(m.group(2).lower()); yr = int(m.group(3))
    if not mon:
        return None
    try:
        import datetime
        return datetime.date(yr, mon, day).isoformat()
    except ValueError:
        return None


def _pdf_reg(t):
    m = _REG_LINE_RE.search(t)
    if not m:
        return None
    raw = m.group(1).strip()
    # first registration token (split off "(Aircraft 1)" etc.)
    tok = re.match(r"([A-Z0-9][A-Z0-9\-]{1,9})", raw.upper())
    return tok.group(1).rstrip("-") if tok else None


def _pdf_aircraft(t):
    m = _AC_LINE_RE.search(t)
    if not m:
        return None
    val = re.sub(r"\s+", " ", m.group(1)).strip()
    val = re.split(r"\(Aircraft", val)[0].strip().rstrip(",")
    if not val or _LABEL_NOISE_RE.search(val):
        return None
    return val


def _pdf_location(t):
    m = _LOC_LINE_RE.search(t)
    if not m:
        return None
    val = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
    # Reject fragments that are clearly another label or mid-sentence text.
    if not val or _LABEL_NOISE_RE.search(val):
        return None
    if len(val.split()) > 9:
        return None
    return val


def parse(conn):
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, date_of_occurrence "
        "FROM baaid_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) <= SCANNED_CEILING:
            # image-only scan (or near-empty) — cannot ingest
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = full_text, "short"

        # Authoritative metadata from PDF text (fall back to listing hint).
        occ = baaid.normalize_occ(full_text)
        reg = _pdf_reg(full_text) or row["registration"]
        ac = _pdf_aircraft(full_text)
        date_iso = _pdf_date_iso(full_text) or row["date_of_occurrence"]
        loc = _pdf_location(full_text)

        # event_class label = OCC number when known (used as report_type later).
        conn.execute(
            "UPDATE baaid_reports SET narrative_text=?, source_tier=?, "
            "registration=COALESCE(?, registration), aircraft=?, "
            "date_of_occurrence=COALESCE(?, date_of_occurrence), location=?, "
            "event_class=COALESCE(?, event_class), status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, reg, ac, date_iso, loc, occ,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, source_tier "
        "FROM baaid_reports WHERE status=?", (db.STATUS_PARSED,)
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR or row["source_tier"] == "scanned":
            conn.execute(
                "UPDATE baaid_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["case_id"], row["aircraft"], row["registration"], row["location"]
        )
        report_type = row["event_class"]  # OCC number when known, else None

        conn.execute(
            "INSERT OR REPLACE INTO baaid_accidents "
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
                "BS",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE baaid_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
