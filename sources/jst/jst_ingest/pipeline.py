# jst_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for JST Argentina.

discover() reads the public manifest (Index.json) and keeps the AE/ —
aeronáutica — entries.  One row per case_id, with the document chosen by the
ISO > IB > INC > IPROV > IP preference and the occurrence date taken from the
manifest path's MMDDYY prefix.

It used to paginate an events API on intranet.jst.gob.ar for that metadata.
That host answers `Disallow: /` — a blanket refusal, on a host named intranet
— while the manifest and the PDFs sit on so.jst.gob.ar under `Allow: /`.  So
the data was always public; the pipeline was reaching it through the wrong
door.

fetch() downloads the chosen PDF, pdftotext, tiers pdf/scanned, and reads the
report's own front matter for aircraft / registration / location / occurrence
type, which the events API used to supply.  A download/extract failure leaves
the row 'new' for retry.  Fatalities are no longer collected: the API summed
them per vehicle and the report header does not print them.

build() promotes 'parsed' rows with narrative >= floor into jst_accidents
(country AR, source_url = the PDF URL, report_type = doc tipo).
"""
import os
import re
import sys
import time

from . import db, jst, pdf
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


def discover(conn, client, max_pages=None, full=False):
    """Enumerate aviation reports from the public manifest; INSERT new rows.

    This used to paginate an events API on intranet.jst.gob.ar, whose
    robots.txt is a blanket `Disallow: /`. Everything it needs is on the public
    host instead: Index.json lists every published report, and the report's own
    front matter carries the metadata that API supplied.

    What each row gets at this stage:

        case_id, doc tipo, PDF URL   the manifest
        date_of_occurrence           the manifest path's MMDDYY prefix, which
                                     1826 of 1828 aviation documents carry

    aircraft, registration, location and occurrence type come from the report
    itself, in fetch(), because they are printed on its first page. That is a
    better source than the old one: it is what the investigators published,
    rather than a separate database that had to agree with it.

    max_pages/full are accepted for API parity — the manifest is a single
    document, so there is nothing to paginate.
    """
    manifest = jst.fetch_manifest(client)
    if not manifest:
        raise RuntimeError(
            "[jst discover] Index.json came back empty — the manifest is the "
            "whole listing, so this is a failure, not an empty source."
        )

    by_case = {}
    for case_id, tipo, path in jst.aviation_docs(manifest):
        by_case.setdefault(case_id, []).append((tipo, path))
    if not by_case:
        raise RuntimeError(
            "[jst discover] the manifest holds no %s documents — either the "
            "mode prefix changed or this is not the aviation manifest."
            % jst.AVIATION_PREFIX
        )

    existing = {
        r["case_id"] for r in conn.execute("SELECT case_id FROM jst_reports")
    }
    inserted = 0
    for case_id, docs in sorted(by_case.items()):
        if case_id in existing:
            continue
        chosen = jst.pick_manifest_doc(docs)
        if not chosen:
            continue
        doc_tipo, doc_path = chosen
        existing.add(case_id)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO jst_reports "
            "(case_id, nro_expediente, doc_path, doc_tipo, aircraft, "
            "registration, operator, occurrence_type, date_of_occurrence, "
            "location, fatalities, summary, pdf_url, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                None,                       # from the report, in fetch()
                doc_path,
                doc_tipo,
                None,                       # aircraft     ┐
                None,                       # registration │ all from the
                None,                       # operator     │ report's own
                None,                       # occurrence   │ front matter
                jst.date_from_path(doc_path),
                None,                       # location     ┘
                None,                       # fatalities: not printed in the header
                None,                       # summary
                jst.pdf_url(doc_path),
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
    For each status='new' row: download the chosen PDF, pdftotext, tier,
    advance to 'parsed'.  Failing rows stay 'new'.
    """
    rows = conn.execute(
        "SELECT case_id, doc_path, pdf_url FROM jst_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_path = os.path.join(pdf_dir, _safe_filename(case_id) + ".pdf")
        url = row["pdf_url"] or jst.pdf_url(row["doc_path"])
        if not url:
            continue
        time.sleep(jst.DELAY)
        try:
            jst.download_pdf(client, url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[jst fetch] {case_id}: {url}: {e}", file=sys.stderr)
            continue  # stays 'new' for retry next cycle

        tier = "pdf" if len(text or "") >= _NARRATIVE_FLOOR else "scanned"

        # The report's own front matter is where aircraft, registration,
        # location and occurrence type come from now. COALESCE keeps whatever
        # discover already knew — the manifest's date — when the header does
        # not repeat it, and lets a re-fetch fill gaps without erasing fields.
        meta = jst.parse_report_header(text or "")
        try:
            conn.execute(
                "UPDATE jst_reports SET narrative_text=?, source_tier=?, "
                "pdf_path=?, aircraft=COALESCE(aircraft, ?), "
                "registration=COALESCE(registration, ?), "
                "location=COALESCE(location, ?), "
                "occurrence_type=COALESCE(occurrence_type, ?), "
                "nro_expediente=COALESCE(nro_expediente, ?), "
                "date_of_occurrence=COALESCE(date_of_occurrence, ?), "
                "status=?, updated_at=? WHERE case_id=?",
                (text, tier, pdf_path,
                 meta["aircraft"], meta["registration"], meta["location"],
                 meta["occurrence_type"], meta["nro_expediente"],
                 meta["date_of_occurrence"],
                 db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[jst fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into jst_accidents."""
    rows = conn.execute(
        "SELECT case_id, doc_tipo, aircraft, registration, operator, "
        "location, date_of_occurrence, narrative_text, pdf_url, summary "
        "FROM jst_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE jst_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO jst_accidents "
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
                "AR",
                narrative,
                None,
                row["pdf_url"] or "https://www.argentina.gob.ar/jst",
                row["doc_tipo"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE jst_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
