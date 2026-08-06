"""The three ways this scraper used to lose reports without saying so.

Every case here is drawn from reading the pipeline against the live site, not
from a hypothetical: OVV publishes reports as several sectional PDFs, its
listing is walked stop-on-empty, and a fetch failure used to be indistinguishable
from a report that genuinely has no usable text.
"""
import pytest

from ovv_ingest import db, ovv, pipeline

from .test_pipeline import FakeClient, FakeResp, _D1, _D2, _DETAIL1, _LISTING, _DOC_MAIN


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _seed_new(conn, case_id, url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ovv_reports (case_id, detail_url, status, discovered_at, "
        "updated_at) VALUES (?,?,?,?,?)",
        (case_id, url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def _status(conn, case_id):
    return conn.execute(
        "SELECT status FROM ovv_reports WHERE case_id=?", (case_id,)
    ).fetchone()["status"]


class _FailingDocs(FakeClient):
    """Detail page loads; every document download raises (a 503, a timeout)."""

    def get(self, url, params=None):
        if url.endswith("-pdf/"):
            raise RuntimeError("HTTP 503")
        return super().get(url, params)


class TestATransientFailureMustNotBePermanent:
    def test_a_row_whose_documents_all_failed_stays_new(self, tmp_path, monkeypatch):
        # This is the expensive one. The row used to be stamped 'parsed' with
        # empty text, build() then moved it to 'skipped', and fetch() only ever
        # reads 'new' — so one timeout retired a report forever, silently.
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)

        pipeline.fetch(conn, _FailingDocs(), str(tmp_path))

        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_NEW

    def test_the_next_cycle_picks_that_row_up_and_succeeds(self, tmp_path, monkeypatch):
        # The point of staying 'new': the weekly cycle self-heals.
        monkeypatch.setattr(ovv, "DELAY", 0)
        monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "R" * 5000)
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)

        pipeline.fetch(conn, _FailingDocs(), str(tmp_path))
        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_NEW

        pipeline.fetch(conn, FakeClient(), str(tmp_path))
        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_PARSED

    def test_metadata_from_the_detail_page_is_still_kept(self, tmp_path, monkeypatch):
        # Staying 'new' must not throw away what the detail page did give us.
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)

        pipeline.fetch(conn, _FailingDocs(), str(tmp_path))

        row = conn.execute(
            "SELECT title, summary FROM ovv_reports WHERE case_id=?",
            ("crash-ph-abc-somewhere",),
        ).fetchone()
        assert row["title"]
        assert row["summary"]

    def test_a_downloadable_but_unreadable_scan_is_still_parsed(self, tmp_path, monkeypatch):
        # The mirror case, and the reason this cannot just retry everything:
        # a report we DID fetch and that genuinely has no text layer must not
        # be retried forever. It is a quality decision, not a transient fault.
        monkeypatch.setattr(ovv, "DELAY", 0)
        monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
        monkeypatch.setattr(pipeline.pdf, "ocr_extract", lambda p, lang=None: "")
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)

        pipeline.fetch(conn, FakeClient(), str(tmp_path))

        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_PARSED


class TestDiscoverMustNotMistakeFailureForTheEnd:
    def test_an_error_mid_walk_is_raised_not_swallowed(self, monkeypatch):
        # A 502 on page 3 used to end the walk and return normally: a truncated
        # crawl reported as a successful one.
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()

        class _DiesOnPage3(FakeClient):
            def get(self, url, params=None):
                if params and params.get("_page") == 3:
                    raise RuntimeError("HTTP 502")
                return FakeResp(text=_LISTING if params else "")

        with pytest.raises(RuntimeError, match="page 3"):
            pipeline.discover(conn, _DiesOnPage3())

    def test_rows_found_before_the_failure_are_kept(self, monkeypatch):
        # Raising must not roll back real work — the next run resumes.
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()

        class _DiesOnPage2(FakeClient):
            def get(self, url, params=None):
                if params and params.get("_page") == 2:
                    raise RuntimeError("HTTP 502")
                return FakeResp(text=_LISTING)

        with pytest.raises(RuntimeError):
            pipeline.discover(conn, _DiesOnPage2())

        assert conn.execute("SELECT COUNT(*) c FROM ovv_reports").fetchone()["c"] == 2

    def test_an_empty_first_page_is_an_error_not_an_empty_source(self, monkeypatch):
        # OVV publishes thousands of investigations. Zero anchors on page 1
        # means the markup changed (the site was redesigned once already),
        # which stop-on-empty reads as "no reports exist".
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()

        class _Redesigned(FakeClient):
            def get(self, url, params=None):
                return FakeResp(text="<html><body><div>nothing we know</div></body></html>")

        with pytest.raises(RuntimeError, match="page 1"):
            pipeline.discover(conn, _Redesigned())

    def test_an_empty_later_page_still_ends_the_walk_quietly(self, monkeypatch):
        # The legitimate end of the listing must stay legitimate.
        monkeypatch.setattr(ovv, "DELAY", 0)
        conn = _conn()
        assert pipeline.discover(conn, FakeClient()) == 2


class TestSectionalDocumentsMustNotOutrankTheReport:
    @pytest.mark.parametrize("name", [
        "ab12cd34ef56recommendations_report_en-pdf",
        "ab12cd34ef56conclusions_en-pdf",
        "ab12cd34ef56summary_report_en-pdf",
        "ab12cd34ef56annex_2_en-pdf",
        "ab12cd34ef56letter_to_minister_en-pdf",
    ])
    def test_an_english_section_ranks_below_a_dutch_full_report(self, name):
        # rank_docs scored English+report highest, and the noise list was
        # Dutch-only, so "recommendations_report_en" beat the actual report.
        # That is where the "5 RECOMMENDATIONS" narratives came from.
        section = f"https://onderzoeksraad.nl/{name}/"
        full = "https://onderzoeksraad.nl/ab12cd34ef99eindrapport_crash-pdf/"
        assert ovv.rank_docs([section, full])[0] == full

    def test_an_english_full_report_still_wins(self):
        # The ranking must not overcorrect: EN main report is still first.
        en = "https://onderzoeksraad.nl/ab12cd34ef56report_crash_en-pdf/"
        nl = "https://onderzoeksraad.nl/ab12cd34ef99rapport_crash-pdf/"
        assert ovv.rank_docs([nl, en])[0] == en

    def test_the_dutch_noise_words_still_work(self):
        app = "https://onderzoeksraad.nl/ab12cd34ef56bijlage_en-pdf/"
        full = "https://onderzoeksraad.nl/ab12cd34ef99rapport-pdf/"
        assert ovv.rank_docs([app, full])[0] == full


class TestASectionIsNotAReport:
    """Ranking is not enough when the section is the ONLY document.

    Measured on the live database: of 386 built rows, 14 hold a section
    rather than a report — narratives beginning "5 RECOMMENDATIONS", "Summary
    Uncontrolled landing in strong winds", "Date 11 December 2018 … To Board".
    Several of those are Dutch words that were already in the noise list
    (aanbevelingen, reactie) and still won, because nothing better was linked.
    Ordering cannot fix that; refusing to store it can.
    """

    def _detail_with_only(self, doc_url):
        return (f'<h1>Crash PH-ABC at Somewhere on 6 June 2021</h1>'
                f'<p>{"S" * 200}</p><a href="{doc_url}">only doc</a>')

    def _fetch_with(self, tmp_path, monkeypatch, doc_url, text):
        monkeypatch.setattr(ovv, "DELAY", 0)
        monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)
        client = FakeClient(urls={_D1: self._detail_with_only(doc_url),
                                  doc_url: b"%PDF"})
        pipeline.fetch(conn, client, str(tmp_path))
        return conn

    def test_a_recommendations_only_investigation_stays_new(self, tmp_path, monkeypatch):
        conn = self._fetch_with(
            tmp_path, monkeypatch,
            "https://onderzoeksraad.nl/ab12aanbevelingen_boeing_767_schiphol-pdf/",
            "5  RECOMMENDATIONS  5.1 Technical facilities with regard to " + "x" * 3000,
        )
        # Not 'parsed': storing a recommendations chapter as the accident
        # narrative is a wrong answer, and 'new' means a later cycle can still
        # pick up the full report when OVV publishes it.
        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_NEW

    def test_a_real_report_of_the_same_length_is_still_stored(self, tmp_path, monkeypatch):
        # The guard keys on the document, not on the text being short — two of
        # the 14 lookalikes are genuine short finals ("FINAL REPORT 96-12/A-5")
        # and must survive.
        conn = self._fetch_with(
            tmp_path, monkeypatch,
            "https://onderzoeksraad.nl/011e_lv_1996_12_a_5_g_jtca_piper_pa23_de_kooij-pdf/",
            "FINAL REPORT 96-12/A-5 G-JTCA, Piper PA23 Aztec " + "x" * 3000,
        )
        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_PARSED

    def test_a_section_alongside_a_report_is_simply_outranked(self, tmp_path, monkeypatch):
        # When both exist the ranking already picks the report, and the row
        # must build normally — the guard must not fire here.
        monkeypatch.setattr(ovv, "DELAY", 0)
        monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "R" * 5000)
        conn = _conn()
        _seed_new(conn, "crash-ph-abc-somewhere", _D1)
        pipeline.fetch(conn, FakeClient(), str(tmp_path))
        assert _status(conn, "crash-ph-abc-somewhere") == db.STATUS_PARSED
