# tests/test_pipeline.py
"""Pipeline integration tests using a real DB (no network)."""
import os
import tempfile
import pytest

from ipiaam_ingest import db
from ipiaam_ingest.pipeline import parse, build


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


# Minimal bilingual PDF text (simulated pdftotext output).
BILINGUAL_TEXT = """\
RELATÓRIO SUMÁRIO DE INCIDENTE GRAVE COM AERONAVE
AIRCRAFT SERIOUS INCIDENT SUMMARY REPORT

001/INCID-A/IPIAAM/2021

Ocorrência / Event:
Separação da porta traseira de passageiros, durante a descolagem

Data / Date
02/11/2021
14:30 CVT

AERONAVE / AIRCRAFT
Tipo / Type         DA42NG-VI
Matrícula / Registration
OE-FUB
Operador / Operator
Aerocália Lda

Fatais / Fatal
0
0
0

DESCRIÇÃO FATUAL / FACTUAL DESCRIPTION
The aircraft door separation occurred during takeoff roll.  During the takeoff
roll at GVAC, the rear passenger door of the DA42NG-VI aircraft with
registration OE-FUB separated from the airframe.  The crew declared an
emergency and landed safely.  No injuries to crew or passengers.  Investigation
conducted under Annex 13 provisions.  The door latch mechanism was found to be
improperly secured prior to flight.  Recommendations issued to operator
regarding pre-flight door checks.
"""

PT_ONLY_TEXT = """\
Relatório sumário de incidente grave. Esta ocorrência foi registada no dia
10/03/2019 em Praia, Cabo Verde. A aeronave do tipo ATR com matrícula D4-CCS
pertencente à empresa TACV efectuou uma aterragem de emergência.
Fatais / Fatal\n0\n0\n0
""" * 20  # repeat to exceed floor


def _seed_fetched(conn, case_id, pdf_path):
    ts = db.now_ms()
    conn.execute(
        'INSERT OR REPLACE INTO ipiaam_reports '
        '(case_id, report_ref, pdf_url, pdf_path, pub_date, lang, status, discovered_at, updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (case_id, case_id.replace('ipiaam-', '').upper(), 'https://example.com/x.pdf',
         pdf_path, '2021-12-23', 'en', db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()


def test_parse_extracts_metadata(tmp_path):
    """parse() correctly populates date_of_occurrence, registration, lang."""
    pdf_path = str(tmp_path / 'test.pdf')
    # Write a fake PDF (pdftotext won't work, but we can monkey-patch).
    open(pdf_path, 'wb').close()

    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        _seed_fetched(conn, 'ipiaam-001-incid-a-2021', pdf_path)

        # Monkey-patch extract_text where pipeline.py imported it.
        import ipiaam_ingest.pipeline as pipeline_mod
        original = pipeline_mod.extract_text
        pipeline_mod.extract_text = lambda _: BILINGUAL_TEXT
        try:
            count = parse(conn)
        finally:
            pipeline_mod.extract_text = original

        assert count == 1
        row = conn.execute(
            'SELECT * FROM ipiaam_reports WHERE case_id=?',
            ('ipiaam-001-incid-a-2021',),
        ).fetchone()
        assert row['status'] == db.STATUS_PARSED
        assert row['date_of_occurrence'] == '2021-11-02'
        assert row['registration'] == 'OE-FUB'
        assert row['lang'] == 'en'
        assert row['fatalities_total'] == 0
        conn.close()
    finally:
        os.unlink(path)


def test_build_emits_accident_row(tmp_path):
    """build() inserts into ipiaam_accidents for well-parsed rows."""
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports '
            '(case_id, report_ref, pdf_url, pdf_path, pub_date, date_of_occurrence, '
            'aircraft, registration, operator, location, narrative_text, source_tier, '
            'fatalities_total, lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                'ipiaam-001-incid-a-2021',
                '001/INCID-A/IPIAAM/2021',
                'https://www.ipiaam.cv/documento/opendoc/1640281158_en.pdf',
                '/some/path.pdf',
                '2021-12-23',
                '2021-11-02',
                'DA42NG-VI',
                'OE-FUB',
                'Aerocália Lda',
                'GVAC',
                BILINGUAL_TEXT,
                'pdf',
                0,
                'en',
                db.STATUS_PARSED,
                ts, ts,
            ),
        )
        conn.commit()

        count = build(conn)
        assert count == 1

        acc = conn.execute(
            'SELECT * FROM ipiaam_accidents WHERE case_id=?',
            ('ipiaam-001-incid-a-2021',),
        ).fetchone()
        assert acc is not None
        assert acc['country'] == 'CV'
        assert acc['event_date'] == '2021-11-02'
        assert acc['registration'] == 'OE-FUB'
        assert acc['report_type'] == 'Serious Incident'
        assert acc['fatalities_total'] == 0
        conn.close()
    finally:
        os.unlink(path)


def test_build_skips_short_narrative():
    """build() skips rows whose narrative_text is below _NARRATIVE_FLOOR."""
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports '
            '(case_id, report_ref, pdf_url, narrative_text, source_tier, '
            'lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (
                'ipiaam-test-skip', '001/INCID-A/IPIAAM/2030',
                'https://x.pdf', 'short', 'scanned', 'pt',
                db.STATUS_PARSED, ts, ts,
            ),
        )
        conn.commit()

        count = build(conn)
        assert count == 0

        row = conn.execute(
            'SELECT status FROM ipiaam_reports WHERE case_id=?',
            ('ipiaam-test-skip',),
        ).fetchone()
        assert row['status'] == db.STATUS_SKIPPED
        conn.close()
    finally:
        os.unlink(path)


def test_build_report_type_incident():
    """report_type is 'Incident' for INCID refs and narrative without SI phrases."""
    # Use a narrative that does NOT mention 'Serious Incident' or 'Incidente Grave'.
    plain_incident_text = (
        "AIRCRAFT INCIDENT SUMMARY REPORT\n"
        "002/INCID/2030\n"
        "Classification: Incident\n"
        "Minor fuel discrepancy reported during preflight inspection.\n"
        "The crew noted a minor anomaly in fuel quantity indication. "
        "Aircraft was returned to maintenance for inspection. "
        "No flight took place and no injuries occurred. "
        "Investigation concluded no safety risk to continued operation. "
        "Recommendations issued regarding fuel gauge calibration procedures.\n"
    ) * 30  # ensure above _NARRATIVE_FLOOR

    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports '
            '(case_id, report_ref, pdf_url, date_of_occurrence, narrative_text, '
            'source_tier, lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                'ipiaam-002-incid-2030', '002/INCID/2030',
                'https://x.pdf', '2030-01-15', plain_incident_text,
                'pdf', 'en', db.STATUS_PARSED, ts, ts,
            ),
        )
        conn.commit()
        build(conn)
        acc = conn.execute(
            'SELECT report_type FROM ipiaam_accidents WHERE case_id=?',
            ('ipiaam-002-incid-2030',),
        ).fetchone()
        assert acc['report_type'] == 'Incident'
        conn.close()
    finally:
        os.unlink(path)


def test_build_report_type_serious_incident_from_narrative():
    """report_type is 'Serious Incident' when narrative contains that phrase,
    even if the ref uses plain INCID (older naming convention like 002/INCID/2020)."""
    path = _tmp_db()
    try:
        conn = db.connect(path)
        db.init_schema(conn)
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports '
            '(case_id, report_ref, pdf_url, date_of_occurrence, narrative_text, '
            'source_tier, lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (
                'ipiaam-002-incid-2020', '002/INCID/2020',
                'https://x.pdf', '2020-09-16', BILINGUAL_TEXT,
                'pdf', 'en', db.STATUS_PARSED, ts, ts,
            ),
        )
        conn.commit()
        build(conn)
        acc = conn.execute(
            'SELECT report_type FROM ipiaam_accidents WHERE case_id=?',
            ('ipiaam-002-incid-2020',),
        ).fetchone()
        # BILINGUAL_TEXT contains "Serious Incident" → upgraded even for plain INCID ref.
        assert acc['report_type'] == 'Serious Incident'
        conn.close()
    finally:
        os.unlink(path)
