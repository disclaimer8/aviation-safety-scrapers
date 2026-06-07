# ttsb_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for TTSB Taiwan.

discover() walks the EN completed-investigations list (5 paginated pages) and
the ZH mirror list, parsing rows by their DETAIL-NODE PREFIX (not the list
path). It pairs EN↔ZH rows (same 149-report set) by occurrence date +
registration. For each EN row it resolves an EN PDF (inline /media link, or —
for older 'More Reports' rows — harvested from the EN detail `/post` page) and
records the matched ZH PDF (also detail-harvested when not inline). New reports
are INSERTed keyed on a derived case_id (media-slug → 'ttsb-{detailId}'; the
TTSB report number, if any, upgrades it at fetch time).

fetch() downloads the EN PDF + pdftotext. If the EN text is a stub
(< ZH_FULL_THRESHOLD) and a matching ZH PDF exists, it ALSO downloads the ZH
full report and prefers it as narrative_text (lang='zh'), keeping the EN
summary in en_summary_text. A report with no usable text layer is tiered
'scanned'. case_id is upgraded to the TTSB/ASC report number when one surfaces
in the text and doesn't collide.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
ttsb_accidents (country TW, lang per the chosen narrative).
"""
import os
import sys
import time

from . import ttsb, db, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300


def _harvest_detail_pdf(client, detail_url):
    """GET a detail `/post` page and return its first /media PDF (or None)."""
    if not detail_url:
        return None
    try:
        html = ttsb.fetch_page(client, detail_url)
    except Exception as e:  # noqa: BLE001
        print(f"[ttsb discover] detail {detail_url}: failed: {e}",
              file=sys.stderr)
        return None
    return ttsb.pdf_from_detail(html)


def discover(conn, client, full=False):
    """
    Walk EN + ZH lists, pair rows, resolve EN/ZH PDFs, INSERT new reports.
    Returns the count of newly inserted reports.
    """
    en_rows, zh_rows = [], []
    for page in range(1, ttsb.NUM_PAGES + 1):
        time.sleep(ttsb.DELAY)
        try:
            html = ttsb.fetch_page(client, ttsb.en_list_url(page))
            en_rows.extend(
                ttsb.parse_listing(html, ttsb.EN_DETAIL_PREFIX, lang="en"))
        except Exception as e:  # noqa: BLE001
            print(f"[ttsb discover] EN page {page}: failed: {e}",
                  file=sys.stderr)
    for page in range(1, ttsb.NUM_PAGES + 1):
        time.sleep(ttsb.DELAY)
        try:
            html = ttsb.fetch_page(client, ttsb.zh_list_url(page))
            zh_rows.extend(
                ttsb.parse_listing(html, ttsb.ZH_DETAIL_PREFIX, lang="zh"))
        except Exception as e:  # noqa: BLE001
            print(f"[ttsb discover] ZH page {page}: failed: {e}",
                  file=sys.stderr)

    pairs = ttsb.match_en_zh(en_rows, zh_rows)

    taken_ids = {
        r["detail_id"]
        for r in conn.execute(
            "SELECT detail_id FROM ttsb_reports WHERE detail_id IS NOT NULL")
    }
    taken_case = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM ttsb_reports WHERE case_id IS NOT NULL")
    }

    inserted = 0
    for e in en_rows:
        detail_id = e["detail_id"]
        if not detail_id or detail_id in taken_ids:
            continue

        z = pairs.get(detail_id)

        # Resolve the EN PDF: inline /media, else harvest from the EN detail.
        en_pdf = e["pdf_url"]
        if not en_pdf:
            time.sleep(ttsb.DELAY)
            en_pdf = _harvest_detail_pdf(client, e["detail_url"])
        # Resolve the ZH PDF: inline /media on the ZH row, else detail-harvest.
        zh_pdf = z["pdf_url"] if z else None
        if z and not zh_pdf:
            time.sleep(ttsb.DELAY)
            zh_pdf = _harvest_detail_pdf(client, z["detail_url"])

        if not en_pdf and not zh_pdf:
            continue  # nothing fetchable for this report yet

        # case_id: media slug → ttsb-{detailId} (report number upgrades later).
        case_id = ttsb.derive_case_id(None, en_pdf or zh_pdf, detail_id)
        cand, n = case_id, 2
        while cand in taken_case:
            cand = f"{case_id}-{n}"
            n += 1
        case_id = cand

        registration = e["registration"] or (z["registration"] if z else None)
        aircraft = e["aircraft"] or (z["aircraft"] if z else None)
        location = e["location"] or (z["location"] if z else None)
        event_date = e["event_date"] or (z["event_date"] if z else None)

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ttsb_reports "
            "(case_id, detail_id, en_detail_url, zh_detail_url, en_pdf_url, "
            "zh_pdf_url, title, report_kind, aircraft, registration, "
            "date_of_occurrence, location, lang, status, discovered_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                detail_id,
                e["detail_url"],
                z["detail_url"] if z else None,
                en_pdf,
                zh_pdf,
                e["title"],
                e["report_kind"],
                aircraft,
                registration,
                event_date,
                location,
                "en",  # provisional; finalised at build from chosen narrative
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        taken_ids.add(detail_id)
        taken_case.add(case_id)
        inserted += 1
    conn.commit()
    return inserted


def _maybe_upgrade_case_id(conn, detail_id, current_case_id, report_no,
                           taken_case):
    """
    Upgrade case_id to the TTSB/ASC report number (priority 1) when it surfaces
    in the PDF text and doesn't collide. Returns the (possibly new) case_id.
    """
    if not report_no:
        return current_case_id
    new_id = report_no.upper()
    if new_id == current_case_id or new_id in taken_case:
        return current_case_id
    if conn.execute(
        "SELECT 1 FROM ttsb_reports WHERE case_id=?", (new_id,)
    ).fetchone():
        return current_case_id
    conn.execute(
        "UPDATE ttsb_reports SET case_id=? WHERE detail_id=?",
        (new_id, detail_id),
    )
    taken_case.discard(current_case_id)
    taken_case.add(new_id)
    return new_id


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: download the EN PDF; if it's a stub and a ZH
    full report exists, also download ZH and prefer it. Choose narrative + lang,
    extract/verify registration, tier pdf/scanned, upgrade case_id.
    """
    rows = conn.execute(
        "SELECT detail_id, case_id, en_pdf_url, zh_pdf_url, registration "
        "FROM ttsb_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    taken_case = {
        r["case_id"]
        for r in conn.execute(
            "SELECT case_id FROM ttsb_reports WHERE case_id IS NOT NULL")
    }
    for row in rows:
        detail_id = row["detail_id"]
        case_id = row["case_id"]
        en_url, zh_url = row["en_pdf_url"], row["zh_pdf_url"]

        en_text, zh_text = "", ""
        en_path = zh_path = None
        try:
            if en_url:
                en_path = os.path.join(pdf_dir, f"{case_id}.en.pdf")
                time.sleep(ttsb.DELAY)
                ttsb.download_pdf(client, en_url, en_path)
                en_text = pdf.extract_text(en_path)
        except Exception as e:  # noqa: BLE001
            print(f"[ttsb fetch] {case_id}: EN pdf failed: {e}",
                  file=sys.stderr)
            continue  # stays 'new', retried next cycle

        # ZH full-report fallback: only when EN is a stub and a ZH PDF exists.
        need_zh = zh_url and len(en_text) < ttsb.ZH_FULL_THRESHOLD
        if need_zh:
            try:
                zh_path = os.path.join(pdf_dir, f"{case_id}.zh.pdf")
                time.sleep(ttsb.DELAY)
                ttsb.download_pdf(client, zh_url, zh_path)
                zh_text = pdf.extract_text(zh_path)
            except Exception as e:  # noqa: BLE001
                print(f"[ttsb fetch] {case_id}: ZH pdf failed: {e}",
                      file=sys.stderr)
                zh_text = ""

        narrative, lang, en_summary = ttsb.choose_narrative(en_text, zh_text)

        tier = "pdf"
        if len(narrative) < _NARRATIVE_FLOOR:
            tier = "scanned"

        # Registration: keep listing value, else recover from chosen text.
        registration = row["registration"] or \
            ttsb.extract_registration(narrative)

        # case_id upgrade from the report number in whichever text we trust.
        report_no = ttsb.report_number(en_text) or ttsb.report_number(zh_text)
        case_id = _maybe_upgrade_case_id(
            conn, detail_id, case_id, report_no, taken_case)

        try:
            conn.execute(
                "UPDATE ttsb_reports SET narrative_text=?, en_summary_text=?, "
                "lang=?, source_tier=?, registration=?, en_pdf_path=?, "
                "zh_pdf_path=?, status=?, updated_at=? WHERE detail_id=?",
                (narrative, en_summary, lang, tier, registration, en_path,
                 zh_path, db.STATUS_PARSED, db.now_ms(), detail_id),
            )
            conn.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[ttsb fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into ttsb_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_id, en_detail_url, report_kind, aircraft, "
        "registration, location, date_of_occurrence, lang, narrative_text, "
        "en_summary_text FROM ttsb_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ttsb_reports SET status=?, updated_at=? "
                "WHERE detail_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["detail_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO ttsb_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, en_summary_text, probable_cause, "
            "source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                None,
                row["location"],
                "TW",
                row["lang"],
                narrative,
                row["en_summary_text"],
                None,
                row["en_detail_url"] or ttsb.BASE,
                row["report_kind"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ttsb_reports SET status=?, updated_at=? WHERE detail_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["detail_id"]),
        )
        conn.commit()
        built += 1
    return built
