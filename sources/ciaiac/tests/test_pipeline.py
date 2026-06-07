# tests/test_pipeline.py
"""
Pipeline tests for ciaiac discover → fetch → parse → build.
"""
import os

from ciaiac_ingest import ciaiac, db, pipeline
from ciaiac_ingest.pdf import MIN_NARRATIVE


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# Fake listing rows returned by parse_listing mock:
#   row0: has both EN + ES pdf → EN preferred
#   row1: ES only
#   row2: no pdf at all (HTML provisional)
_FAKE_ROWS = [
    {
        "case_id": "A-005/2024",
        "report_url": "https://www.transportes.gob.es/report/a-005-2024",
        "pdf_url_es": "https://www.transportes.gob.es/pdfs/a-005-2024_es.pdf",
        "pdf_url_en": "https://www.transportes.gob.es/pdfs/a-005-2024_en.pdf",
        "event_class": "Accident",
        "aircraft": "AIRBUS A320",
        "registration": "EC-XYZ",
        "date_of_occurrence": "2024-03-15",
        "location": "Aeropuerto de Madrid",
        "title": "15 de marzo de 2024. Aeronave AIRBUS A320, matrícula EC-XYZ. Madrid. Ref. A-005/2024",
    },
    {
        "case_id": "IN-002/2024",
        "report_url": "https://www.transportes.gob.es/report/in-002-2024",
        "pdf_url_es": "https://www.transportes.gob.es/pdfs/in-002-2024_es.pdf",
        "pdf_url_en": None,
        "event_class": "Serious incident",
        "aircraft": "BOEING 737",
        "registration": "EC-ABC",
        "date_of_occurrence": "2024-01-08",
        "location": "Barcelona",
        "title": "8 de enero de 2024. Aeronave BOEING 737, matrícula EC-ABC. Barcelona. Ref. IN-002/2024",
    },
    {
        "case_id": "A-099/2024",
        "report_url": None,
        "pdf_url_es": None,
        "pdf_url_en": None,
        "event_class": "Accident",
        "aircraft": "CESSNA 172",
        "registration": "EC-NNN",
        "date_of_occurrence": "2024-06-01",
        "location": "Sevilla",
        "title": "1 de junio de 2024. Aeronave CESSNA 172, matrícula EC-NNN. Sevilla. Ref. A-099/2024",
    },
]

_YEAR_URL = "https://www.transportes.gob.es/organos-colegiados/ciaiac/investigacion/2024"


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, index_html="<index>", year_html="<year>"):
        self._index_html = index_html
        self._year_html = year_html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == ciaiac.INDEX_URL:
            return _FakeResp(self._index_html)
        return _FakeResp(self._year_html)


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_new_rows(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaiac, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(ciaiac, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(ciaiac, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3

    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_es, pdf_url_en, lang, status, "
        "event_class, aircraft, registration, date_of_occurrence, location "
        "FROM ciaiac_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["status"] == db.STATUS_NEW for r in rows)

    # row0 (A-005/2024): EN-preference → pdf_url = EN url, lang = 'en'
    r_a005 = next(r for r in rows if r["case_id"] == "A-005/2024")
    assert r_a005["pdf_url"] == "https://www.transportes.gob.es/pdfs/a-005-2024_en.pdf"
    assert r_a005["lang"] == "en"
    assert r_a005["pdf_url_es"] == "https://www.transportes.gob.es/pdfs/a-005-2024_es.pdf"
    assert r_a005["pdf_url_en"] == "https://www.transportes.gob.es/pdfs/a-005-2024_en.pdf"
    assert r_a005["event_class"] == "Accident"
    assert r_a005["aircraft"] == "AIRBUS A320"
    assert r_a005["registration"] == "EC-XYZ"
    assert r_a005["date_of_occurrence"] == "2024-03-15"
    assert r_a005["location"] == "Aeropuerto de Madrid"

    # row1 (IN-002/2024): ES only → pdf_url = ES url, lang = 'es'
    r_in002 = next(r for r in rows if r["case_id"] == "IN-002/2024")
    assert r_in002["pdf_url"] == "https://www.transportes.gob.es/pdfs/in-002-2024_es.pdf"
    assert r_in002["lang"] == "es"
    assert r_in002["pdf_url_en"] is None

    # row2 (A-099/2024): no pdf → pdf_url = None, lang = None
    r_a099 = next(r for r in rows if r["case_id"] == "A-099/2024")
    assert r_a099["pdf_url"] is None
    assert r_a099["lang"] is None


def test_discover_idempotent(monkeypatch):
    """Running discover twice inserts nothing on the second run."""
    conn = _conn()
    monkeypatch.setattr(ciaiac, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(ciaiac, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(ciaiac, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 3
    assert pipeline.discover(conn, client) == 0  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM ciaiac_reports").fetchone()[0] == 3


def test_discover_skips_existing_case_id(monkeypatch):
    conn = _conn()
    # Pre-insert one case_id
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiac_reports (case_id, status, discovered_at, updated_at) VALUES (?,?,?,?)",
        ("A-005/2024", db.STATUS_NEW, ts, ts),
    )
    conn.commit()

    monkeypatch.setattr(ciaiac, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(ciaiac, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(ciaiac, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client) == 2  # only 2 new


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(ciaiac, "iter_year_urls", lambda html: [_YEAR_URL])
    monkeypatch.setattr(ciaiac, "parse_listing", lambda html, year_url="": _FAKE_ROWS)
    monkeypatch.setattr(ciaiac, "DELAY", 0)
    client = _FakeClient()

    assert pipeline.discover(conn, client, full=True) == 3


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiac_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "A-005/2024", "https://transportes.gob.es/pdfs/a-005.pdf")

    download_calls = []
    def _fake_download(client, url, dest):
        download_calls.append((url, dest))
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(ciaiac, "download", _fake_download)
    monkeypatch.setattr(ciaiac, "DELAY", 0)

    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 1

    row = conn.execute(
        "SELECT status, pdf_path FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is not None
    assert os.path.exists(row["pdf_path"])
    assert len(download_calls) == 1


def test_fetch_no_pdf_url_advances_with_null_path(monkeypatch, tmp_path):
    """Row with no pdf_url → advanced to 'fetched', pdf_path stays None."""
    conn = _conn()
    _seed_new(conn, "A-099/2024", None)

    download_calls = []
    monkeypatch.setattr(ciaiac, "download", lambda *a: download_calls.append(a))
    monkeypatch.setattr(ciaiac, "DELAY", 0)

    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 1
    assert not download_calls  # download must NOT be called

    row = conn.execute(
        "SELECT status, pdf_path FROM ciaiac_reports WHERE case_id='A-099/2024'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_keeps_row_new(monkeypatch, tmp_path):
    """Download failure → row stays at 'new', not 'fetched'."""
    conn = _conn()
    _seed_new(conn, "A-005/2024", "https://transportes.gob.es/pdfs/a-005.pdf")

    monkeypatch.setattr(
        ciaiac,
        "download",
        lambda client, url, dest: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    monkeypatch.setattr(ciaiac, "DELAY", 0)

    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 1  # still iterated 1 row

    row = conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()
    assert row["status"] == db.STATUS_NEW  # stayed new


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    """One failing row must not abort subsequent rows."""
    conn = _conn()
    _seed_new(conn, "A-005/2024", "https://transportes.gob.es/pdfs/a-005.pdf")
    _seed_new(conn, "IN-002/2024", "https://transportes.gob.es/pdfs/in-002.pdf")

    def _selective_download(client, url, dest):
        if "a-005" in url:
            raise RuntimeError("403 Forbidden")
        open(dest, "wb").write(b"%PDF")

    monkeypatch.setattr(ciaiac, "download", _selective_download)
    monkeypatch.setattr(ciaiac, "DELAY", 0)

    result = pipeline.fetch(conn, None, str(tmp_path))
    assert result == 2

    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()["status"] == db.STATUS_NEW  # failed → still new

    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='IN-002/2024'"
    ).fetchone()["status"] == db.STATUS_FETCHED  # ok → fetched


# ── parse ─────────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiac_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "A-005/2024", pdf_path="a-005.pdf")

    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)

    assert pipeline.parse(conn) == 1

    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_short_pdf_tier(monkeypatch):
    """PDF text below MIN_NARRATIVE but non-empty → tier='short', text preserved."""
    conn = _conn()
    _seed_fetched(conn, "A-005/2024", pdf_path="a-005.pdf")

    short_text = "Short narrative only 50 chars here."
    monkeypatch.setattr(pipeline, "extract_text", lambda p: short_text)

    assert pipeline.parse(conn) == 1

    row = conn.execute(
        "SELECT narrative_text, source_tier FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()
    assert row["source_tier"] == "short"
    assert row["narrative_text"] == short_text


def test_parse_no_pdf_path(monkeypatch):
    """Row with no PDF path → empty narrative, tier='none', extract_text not called."""
    conn = _conn()
    _seed_fetched(conn, "A-099/2024", pdf_path=None)

    extract_calls = []
    monkeypatch.setattr(
        pipeline, "extract_text", lambda p: extract_calls.append(p) or "X" * 1000
    )

    assert pipeline.parse(conn) == 1
    assert not extract_calls  # must NOT be called when no pdf_path

    row = conn.execute(
        "SELECT narrative_text, source_tier, status FROM ciaiac_reports WHERE case_id='A-099/2024'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "none"
    assert row["narrative_text"] == ""


def test_parse_empty_pdf_extraction(monkeypatch):
    """extract_text returns '' (e.g. pdftotext failure) → tier='none'."""
    conn = _conn()
    _seed_fetched(conn, "A-005/2024", pdf_path="a-005.pdf")

    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")

    pipeline.parse(conn)
    row = conn.execute(
        "SELECT source_tier FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()
    assert row["source_tier"] == "none"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None,
                 location=None, date=None, narrative="", event_class=None,
                 operator=None, pdf_url=None, report_url=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaiac_reports "
        "(case_id, aircraft, registration, location, date_of_occurrence, "
        "narrative_text, event_class, operator, pdf_url, report_url, "
        "status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id, aircraft, registration, location, date,
            narrative, event_class, operator, pdf_url, report_url,
            db.STATUS_PARSED, ts, ts,
        ),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    long_narrative = "N" * 200
    _seed_parsed(
        conn,
        "A-005/2024",
        aircraft="AIRBUS A320",
        registration="EC-XYZ",
        location="Aeropuerto de Madrid",
        date="2024-03-15",
        narrative=long_narrative,
        event_class="Accident",
        operator="Iberia",
        pdf_url="https://transportes.gob.es/pdfs/a-005-2024_en.pdf",
    )

    assert pipeline.build(conn) == 1

    acc = conn.execute(
        "SELECT * FROM ciaiac_accidents WHERE case_id='A-005/2024'"
    ).fetchone()
    assert acc is not None
    assert acc["country"] == "ES"
    assert acc["event_date"] == "2024-03-15"
    assert acc["aircraft"] == "AIRBUS A320"
    assert acc["registration"] == "EC-XYZ"
    assert acc["operator"] == "Iberia"
    assert acc["report_type"] == "Accident"
    assert acc["narrative_text"] == long_narrative
    assert acc["probable_cause"] is None
    assert acc["source_url"] == "https://transportes.gob.es/pdfs/a-005-2024_en.pdf"
    assert acc["site_slug"].startswith("crash-")

    # staging row advances to 'built'
    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-005/2024'"
    ).fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    """When pdf_url is None, source_url should be report_url."""
    conn = _conn()
    _seed_parsed(
        conn, "A-099/2024",
        aircraft="CESSNA 172",
        registration="EC-NNN",
        location="Sevilla",
        date="2024-06-01",
        narrative="N" * 200,
        event_class="Accident",
        pdf_url=None,
        report_url="https://www.transportes.gob.es/report/a-099-2024",
    )
    pipeline.build(conn)
    acc = conn.execute(
        "SELECT source_url FROM ciaiac_accidents WHERE case_id='A-099/2024'"
    ).fetchone()
    assert acc["source_url"] == "https://www.transportes.gob.es/report/a-099-2024"


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(
        conn, "A-001/2024",
        aircraft="BOEING 737",
        narrative="",
        event_class="Accident",
        pdf_url="https://transportes.gob.es/pdfs/a-001.pdf",
    )

    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-001/2024'"
    ).fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM ciaiac_accidents").fetchone()[0] == 0


def test_build_skips_below_narrative_floor():
    """Narrative of exactly _NARRATIVE_FLOOR - 1 chars → skipped."""
    conn = _conn()
    _seed_parsed(
        conn, "A-002/2024",
        aircraft="AIRBUS A320",
        narrative="X" * 79,  # one below the 80-char floor
        event_class="Serious incident",
    )

    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-002/2024'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_es():
    conn = _conn()
    _seed_parsed(
        conn, "A-005/2024",
        aircraft="AIRBUS A320",
        narrative="N" * 200,
        event_class="Accident",
    )
    pipeline.build(conn)
    acc = conn.execute(
        "SELECT country FROM ciaiac_accidents WHERE case_id='A-005/2024'"
    ).fetchone()
    assert acc["country"] == "ES"


def test_build_report_type_from_event_class():
    conn = _conn()
    _seed_parsed(
        conn, "IN-002/2024",
        aircraft="BOEING 737",
        narrative="N" * 200,
        event_class="Serious incident",
    )
    pipeline.build(conn)
    acc = conn.execute(
        "SELECT report_type FROM ciaiac_accidents WHERE case_id='IN-002/2024'"
    ).fetchone()
    assert acc["report_type"] == "Serious incident"


def test_build_mixed_rows():
    """Two buildable rows + one skipped → build() returns 2."""
    conn = _conn()
    long_narr = "Z" * 200

    _seed_parsed(conn, "A-001/2024", aircraft="A320", narrative=long_narr,
                 event_class="Accident")
    _seed_parsed(conn, "A-002/2024", aircraft="B737", narrative=long_narr,
                 event_class="Serious incident")
    _seed_parsed(conn, "A-003/2024", aircraft="C172", narrative="",
                 event_class="Accident")

    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM ciaiac_accidents").fetchone()[0] == 2
    assert conn.execute(
        "SELECT status FROM ciaiac_reports WHERE case_id='A-003/2024'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_build_null_metadata_full_narrative_is_built():
    """Row with null registration, aircraft but full narrative → built."""
    conn = _conn()
    long_narr = "N" * 700
    _seed_parsed(conn, "A-010/2024", narrative=long_narr, event_class=None)

    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM ciaiac_accidents WHERE case_id='A-010/2024'"
    ).fetchone()
    assert acc is not None
    assert acc["narrative_text"] == long_narr
    assert acc["aircraft"] is None
    assert acc["registration"] is None
