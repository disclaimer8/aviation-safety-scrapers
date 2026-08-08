"""Pipeline state machine, with a fake browser page. No network, no Chrome."""
import pytest

from rosap_ingest import db, pipeline, rosap


class FakePage:
    """Stands in for a Playwright page: serves scripted anchor lists."""

    def __init__(self, pages):
        self.pages = pages          # offset -> list of (href, title)
        self.visited = []

    def goto(self, url, **kw):
        self.visited.append(url)
        self._offset = 0
        if "start=" in url:
            self._offset = int(url.rsplit("start=", 1)[1])

    def wait_for_timeout(self, _ms):
        pass

    def eval_on_selector_all(self, _sel, _js):
        return self.pages.get(self._offset, [])


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _title(op, loc, date, suffix=""):
    return f"Investigation of Aircraft Accident: {op}: {loc}: {date}{suffix}"


def _page_of(n, start_pid=40000):
    return [(f"/view/dot/{start_pid + i}",
             _title("UNITED AIRLINES", "BOSTON, MASSACHUSETTS", "1964-03-10"))
            for i in range(n)]


class TestDiscover:
    def test_it_inserts_what_the_listing_offers(self, monkeypatch):
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        page = FakePage({0: _page_of(20), 20: _page_of(5, 41000)})
        assert pipeline.discover(conn, page) == 25

    def test_it_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        page = FakePage({0: _page_of(20), 20: _page_of(5, 41000)})
        pipeline.discover(conn, page)
        assert pipeline.discover(conn, page) == 0

    def test_an_empty_first_page_raises(self, monkeypatch):
        # The collection holds 791 items and will not empty. Zero anchors
        # means the markup moved — which stop-on-empty would report as a
        # clean, complete run.
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        with pytest.raises(RuntimeError, match="0 item anchors"):
            pipeline.discover(_conn(), FakePage({0: []}))

    def test_a_short_last_page_ends_the_walk_quietly(self, monkeypatch):
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        page = FakePage({0: _page_of(11)})
        assert pipeline.discover(conn, page) == 11
        assert len(page.visited) == 1

    def test_max_pages_caps_a_smoke_run(self, monkeypatch):
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        page = FakePage({0: _page_of(20), 20: _page_of(20, 41000)})
        assert pipeline.discover(conn, page, max_pages=1) == 20

    def test_the_title_parts_are_stored(self, monkeypatch):
        monkeypatch.setattr(rosap, "DELAY", 0)
        conn = _conn()
        pipeline.discover(conn, FakePage({0: [
            ("/view/dot/33704", _title("SLICK AIRWAYS", "BOSTON, MASSACHUSETTS",
                                       "1964-03-10"))]}))
        row = conn.execute("SELECT * FROM rosap_reports").fetchone()
        assert (row["operator"], row["date_of_occurrence"], row["doc_kind"]) == \
               ("SLICK AIRWAYS", "1964-03-10", "report")


class TestParseAlwaysOcrs:
    def _fetched(self, conn, pid="33704"):
        conn.execute(
            "INSERT INTO rosap_reports (pid, doc_kind, date_of_occurrence, "
            "location, pdf_path, status, updated_at) VALUES (?,?,?,?,?,?,?)",
            (pid, "report", "1964-03-10", "BOSTON, MASSACHUSETTS",
             "/nonexistent.pdf", db.STATUS_FETCHED, 0))
        conn.commit()

    def test_a_missing_file_yields_the_scanned_tier(self, monkeypatch):
        conn = _conn()
        self._fetched(conn)
        pipeline.parse(conn)
        row = conn.execute("SELECT source_tier, status FROM rosap_reports").fetchone()
        assert row["source_tier"] == "scanned"
        assert row["status"] == db.STATUS_PARSED

    def test_no_ocr_mode_still_advances_the_row(self):
        # CI has no OCR host; the pipeline must not stall there.
        conn = _conn()
        self._fetched(conn)
        assert pipeline.parse(conn, enable_ocr=False) == 1
        assert conn.execute("SELECT status FROM rosap_reports").fetchone()[0] \
               == db.STATUS_PARSED


class TestBuild:
    def _parsed(self, conn, pid, kind, narrative, location="BOSTON, MASSACHUSETTS"):
        conn.execute(
            "INSERT INTO rosap_reports (pid, doc_kind, date_of_occurrence, "
            "operator, location, narrative_text, status, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pid, kind, "1954-07-27", "AMERICAN AIRLINES", location,
             narrative, db.STATUS_PARSED, 0))
        conn.commit()

    def test_a_report_with_a_narrative_is_built(self):
        conn = _conn()
        self._parsed(conn, "1", "report", "N" * 500)
        assert pipeline.build(conn) == 1
        row = conn.execute("SELECT * FROM rosap_accidents").fetchone()
        assert row["case_id"] == "rosap-1"
        assert row["country"] == "US"
        assert row["event_date"] == "1954-07-27"

    @pytest.mark.parametrize("kind", ["Amendment", "Hearing Notice",
                                      "Letter from W. K. Andrews"])
    def test_a_supplement_is_skipped_not_built(self, kind):
        # Five of these belong to one 1954 accident. Building them would put
        # four accidents in the corpus that never happened.
        conn = _conn()
        self._parsed(conn, "1", kind, "N" * 500)
        assert pipeline.build(conn) == 0
        assert conn.execute("SELECT status FROM rosap_reports").fetchone()[0] \
               == db.STATUS_SKIPPED

    def test_an_ocr_pass_that_recovered_nothing_is_skipped(self):
        conn = _conn()
        self._parsed(conn, "1", "report", "")
        assert pipeline.build(conn) == 0

    def test_a_foreign_location_leaves_country_unset(self):
        conn = _conn()
        self._parsed(conn, "1", "report", "N" * 500, location="SHANNON, IRELAND")
        pipeline.build(conn)
        assert conn.execute("SELECT country FROM rosap_accidents").fetchone()[0] is None

    def test_registration_is_never_written(self):
        # It is the top rung of the occurrence dedup ladder. The OCR text names
        # it, but a wrong value there merges unrelated occurrences, so it waits
        # for a quarantine column and cross-source confirmation.
        conn = _conn()
        self._parsed(conn, "1", "report", "N384 " * 200)
        pipeline.build(conn)
        assert conn.execute("SELECT registration FROM rosap_accidents").fetchone()[0] is None

    def test_building_twice_does_not_duplicate(self):
        conn = _conn()
        self._parsed(conn, "1", "report", "N" * 500)
        pipeline.build(conn)
        conn.execute("UPDATE rosap_reports SET status=?", (db.STATUS_PARSED,))
        conn.commit()
        pipeline.build(conn)
        assert conn.execute("SELECT COUNT(*) FROM rosap_accidents").fetchone()[0] == 1
