"""AHAC pipeline tests. No network: the client is a fake.

The package shipped with an empty tests/ directory, so these also serve as its
first coverage of the listing parser.
"""
import pytest

from ahac_ingest import ahac, db, pipeline


class FakeResp:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_HREF = ("Documentos/ACCIDENTES%20E%20INCIDENTES/DOCUMENTO/"
         "Informe%20Final%20del%20accidente%20de%20la%20aeronave%20"
         "con%20matricula%20HR-AQS.pdf")
_LISTING = f'<table><tr><td><a href="{_HREF}">Informe</a></td></tr></table>'


class FakeClient:
    def __init__(self, body=_LISTING):
        self.body = body

    def get(self, url, headers=None):
        return FakeResp(self.body.encode("utf-8"))


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


class TestTheListing:
    def test_it_finds_a_document_link(self):
        rows = ahac.parse_listing(_LISTING)
        assert len(rows) == 1
        assert rows[0]["pdf_url"].startswith("https://ahac.gob.hn/Documentos/")

    def test_the_case_id_comes_from_the_filename(self):
        rows = ahac.parse_listing(_LISTING)
        assert rows[0]["case_id"] == (
            "ahac-informe-final-del-accidente-de-la-aeronave-con-matricula-hr-aqs"
        )

    def test_the_same_document_twice_yields_one_row(self):
        rows = ahac.parse_listing(_LISTING + _LISTING)
        assert len(rows) == 1

    def test_guidance_and_notification_documents_are_skipped(self):
        html = ('<a href="Documentos/ACCIDENTES%20E%20INCIDENTES/DOCUMENTO/'
                'NOTIFICACION%20de%20algo.pdf">n</a>')
        assert ahac.parse_listing(html) == []

    def test_an_accident_and_an_incident_are_classified_apart(self):
        acc = ahac.parse_listing(_LISTING)[0]
        inc_html = ('<a href="Documentos/ACCIDENTES%20E%20INCIDENTES/DOCUMENTO/'
                    'Informe%20del%20incidente%20de%20la%20aeronave%20TG-JOC.pdf">i</a>')
        inc = ahac.parse_listing(inc_html)[0]
        assert acc["event_class"] == "Accident"
        assert inc["event_class"] == "Incident"


class TestDiscoverMustNoticeAnEmptyListing:
    def test_zero_links_from_a_served_page_is_an_error(self):
        # The href pattern is pinned to a literal percent-encoded path
        # (…/ACCIDENTES%20E%20INCIDENTES/DOCUMENTO/). If AHAC re-encodes or
        # moves that directory, the regex matches nothing and discover used to
        # insert zero rows and report success — the source would go quiet and
        # nobody would know.
        conn = _conn()
        with pytest.raises(RuntimeError, match="0 document links"):
            pipeline.discover(conn, FakeClient("<html><body>redesigned</body></html>"))

    def test_a_normal_page_still_inserts(self):
        conn = _conn()
        assert pipeline.discover(conn, FakeClient()) == 1

    def test_a_second_run_inserts_nothing_and_does_not_raise(self):
        # Idempotence must not trip the tripwire: links are found, they are
        # simply already known.
        conn = _conn()
        pipeline.discover(conn, FakeClient())
        assert pipeline.discover(conn, FakeClient()) == 0


class TestParseRecoversTheOccurrenceDate:
    def _fetched_row(self, conn, narrative):
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ahac_reports (case_id, pdf_url, status, discovered_at, "
            "updated_at, pdf_path) VALUES (?,?,?,?,?,?)",
            ("ahac-x", "https://ahac.gob.hn/x.pdf", db.STATUS_FETCHED, ts, ts, "/x.pdf"),
        )
        conn.commit()

    def test_the_date_is_written_when_the_text_states_it(self, monkeypatch):
        conn = _conn()
        self._fetched_row(conn, None)
        text = ("Relato. El accidente ocurrió en La Lima, departamento de Cortés, "
                "el día 20 de enero del año 2016, aproximadamente a las 1530 UTC. "
                + "x" * 700)
        monkeypatch.setattr(pipeline, "extract_text", lambda p: text)

        pipeline.parse(conn)

        row = conn.execute(
            "SELECT date_of_occurrence FROM ahac_reports WHERE case_id='ahac-x'"
        ).fetchone()
        assert row["date_of_occurrence"] == "2016-01-20"

    def test_a_text_with_no_stated_date_leaves_it_null(self, monkeypatch):
        conn = _conn()
        self._fetched_row(conn, None)
        monkeypatch.setattr(pipeline, "extract_text", lambda p: "Sin fecha alguna. " + "x" * 700)

        pipeline.parse(conn)

        row = conn.execute(
            "SELECT date_of_occurrence FROM ahac_reports WHERE case_id='ahac-x'"
        ).fetchone()
        assert row["date_of_occurrence"] is None

    def test_a_date_already_known_is_not_overwritten(self, monkeypatch):
        # If the listing or an earlier pass ever supplies a date, the text is
        # the fallback, not the authority.
        conn = _conn()
        self._fetched_row(conn, None)
        conn.execute("UPDATE ahac_reports SET date_of_occurrence='2001-01-01' "
                     "WHERE case_id='ahac-x'")
        conn.commit()
        text = ("el día 20 de enero del año 2016, aproximadamente a las 1530 UTC. "
                + "x" * 700)
        monkeypatch.setattr(pipeline, "extract_text", lambda p: text)

        pipeline.parse(conn)

        row = conn.execute(
            "SELECT date_of_occurrence FROM ahac_reports WHERE case_id='ahac-x'"
        ).fetchone()
        assert row["date_of_occurrence"] == "2001-01-01"
