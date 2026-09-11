"""Pipeline tests for caav discover → fetch → parse → build."""
import os

from caav_ingest import caav, db, pipeline
from caav_ingest.pdf import MIN_NARRATIVE


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── discover ────────────────────────────────────────────────────────────────

_LISTING_ROWS = [
    {"detail_url": "https://english.caa.gov.vn/doc-detail/r1.htm",
     "listing_title": "Report 1", "pub_date": "2020-01-01"},
    {"detail_url": "https://english.caa.gov.vn/doc-detail/r2.htm",
     "listing_title": "Report 2", "pub_date": "2021-02-02"},
]

_DETAILS = {
    "https://english.caa.gov.vn/doc-detail/r1.htm": {
        "title": "FINAL REPORT BELL 505 VN-8650 ON 05/04/2023",
        "registration": "VN-8650", "date_iso": "2023-04-05",
        "aircraft": "Bell 505",
        "pdf_url": "https://imgcaa.minhvujsc.com/a.pdf",
    },
    "https://english.caa.gov.vn/doc-detail/r2.htm": {
        "title": "FINAL REPORT no reg no date",
        "registration": None, "date_iso": None, "aircraft": None,
        "pdf_url": "https://imgcaa.minhvujsc.com/b.pdf",
    },
}


class _FakeResp:
    def __init__(self, body=""):
        self.content = body.encode("utf-8") if isinstance(body, str) else body

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return _FakeResp("<html>" + url + "</html>")


def _patch(monkeypatch):
    monkeypatch.setattr(caav, "iter_listing_urls", lambda *a, **k: [caav.INDEX_URL])
    monkeypatch.setattr(caav, "parse_listing", lambda html, listing_url="": list(_LISTING_ROWS))
    monkeypatch.setattr(caav, "parse_detail", lambda html: _DETAILS[html.replace("<html>", "").replace("</html>", "")])
    monkeypatch.setattr(caav, "DELAY", 0)


def test_discover_inserts_rows(monkeypatch):
    conn = _conn()
    _patch(monkeypatch)
    assert pipeline.discover(conn, _FakeClient()) == 2

    rows = conn.execute(
        "SELECT case_id, registration, date_of_occurrence, aircraft, pdf_url, "
        "report_url, lang, status FROM caav_reports ORDER BY case_id"
    ).fetchall()
    assert len(rows) == 2
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    assert all(r["lang"] == "en" for r in rows)

    r1 = next(r for r in rows if r["registration"] == "VN-8650")
    assert r1["case_id"] == "VN-8650-2023-04-05"
    assert r1["date_of_occurrence"] == "2023-04-05"
    assert r1["aircraft"] == "Bell 505"
    assert r1["report_url"].endswith("r1.htm")

    # r2: no reg/date → sha1 fallback case_id from pdf_url
    r2 = next(r for r in rows if r["registration"] is None)
    assert r2["case_id"].startswith("VN-")
    assert r2["case_id"] != "VN-8650-2023-04-05"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    _patch(monkeypatch)
    assert pipeline.discover(conn, _FakeClient()) == 2
    assert pipeline.discover(conn, _FakeClient()) == 0
    assert conn.execute("SELECT COUNT(*) FROM caav_reports").fetchone()[0] == 2


def test_discover_full_flag_accepted(monkeypatch):
    conn = _conn()
    _patch(monkeypatch)
    assert pipeline.discover(conn, _FakeClient(), full=True) == 2


# ── discover: ID-enumeration path (Finding 1) ────────────────────────────────

class _EnumClient:
    """Serves a real listing + per-id detail pages for ID-enumeration tests.

    detail_map: {doc_id: html} ; ids absent from the map return the empty-shell
    sentinel (no PDF).  The listing returns server-rendered rows for seed_ids.
    """
    def __init__(self, seed_ids, detail_map):
        self.detail_map = detail_map
        rows = "".join(
            f"<tr><td><a href='/doc-detail/r-{i}.htm'>Seed {i}</a></td>"
            f"<td>01.02.2020</td><td>01.02.2020</td></tr>"
            for i in seed_ids
        )
        self.listing_html = "<table>" + rows + "</table>"
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if url == caav.INDEX_URL:
            return _FakeResp(self.listing_html)
        did = caav.detail_id_from_url(url)
        if did is not None and did in self.detail_map:
            return _FakeResp(self.detail_map[did])
        return _FakeResp("<html>empty shell, no pdf</html>")  # sentinel


def _report_html(reg, date, title="FINAL REPORT - FINAL INVESTIGATION REPORT"):
    pdf = f"https://imgcaa.minhvujsc.com/{reg or 'none'}.pdf"
    t = f"{title} AIRCRAFT {reg} DATED {date}" if reg else title
    return (f"<html><meta property='og:title' content='{t}'>"
            f"<a href='{pdf}'>pdf</a></html>")


def test_discover_id_enum_reaches_reports_beyond_page1(monkeypatch):
    """Reports only reachable by ID-enum (not on the listing) get inserted."""
    monkeypatch.setattr(caav, "DELAY", 0)
    seed = [30245]
    # report on page-1 seed + two reports ONLY reachable by id-enum
    detail = {
        30245: _report_html("VN-A639", "16/10/2020"),
        30240: _report_html("VN-B218", "19/12/2021"),   # below seed, in window
        30231: _report_html("VN-A870", "02/04/2020"),   # below seed, in window
        30246: _report_html(None, None, title="AC 04-009 - GUIDANCE"),  # advisory
    }
    conn = _conn()
    client = _EnumClient(seed, detail)
    inserted = pipeline.discover(conn, client)
    cids = {r["case_id"] for r in conn.execute("SELECT case_id FROM caav_reports")}
    assert "VN-A639-2020-10-16" in cids
    assert "VN-B218-2021-12-19" in cids
    assert "VN-A870-2020-04-02" in cids
    # advisory circular (PDF but non-report title) excluded
    assert inserted == 3
    assert not any("04" in c and "009" in c for c in cids)


def test_discover_id_enum_excludes_empty_shells(monkeypatch):
    """Empty-shell sentinel ids (no PDF) are never inserted."""
    monkeypatch.setattr(caav, "DELAY", 0)
    conn = _conn()
    # only one real report; the rest of the window are empty shells
    client = _EnumClient([30245], {30245: _report_html("VN-A639", "16/10/2020")})
    inserted = pipeline.discover(conn, client)
    assert inserted == 1


def test_discover_id_enum_keeps_titleless_with_pdf(monkeypatch):
    monkeypatch.setattr(caav, "DELAY", 0)
    conn = _conn()
    pdf = "https://imgcaa.minhvujsc.com/titleless.pdf"
    titleless = f"<html><a href='{pdf}'>pdf</a></html>"  # no og:title, no <title>
    client = _EnumClient([30245], {
        30245: _report_html("VN-A639", "16/10/2020"),
        30251: titleless,
    })
    inserted = pipeline.discover(conn, client)
    # both the titled report and the titleless-with-pdf row are kept
    assert inserted == 2
    sha = caav.make_case_id(None, None, pdf)
    cids = {r["case_id"] for r in conn.execute("SELECT case_id FROM caav_reports")}
    assert sha in cids


def test_discover_forward_scan_stops_on_empty_run(monkeypatch):
    """Forward scan stops after ID_FWD_EMPTY_STOP consecutive empty shells."""
    monkeypatch.setattr(caav, "DELAY", 0)
    monkeypatch.setattr(caav, "ID_WINDOW_FWD", 50)
    monkeypatch.setattr(caav, "ID_FWD_EMPTY_STOP", 3)
    conn = _conn()
    seed = [30245]
    client = _EnumClient(seed, {30245: _report_html("VN-A639", "16/10/2020")})
    pipeline.discover(conn, client)
    # forward ids visited should stop ~3 past the seed, not all 50
    fwd_calls = [u for u in client.calls
                 if (caav.detail_id_from_url(u) or 0) > 30245]
    assert len(fwd_calls) <= caav.ID_FWD_EMPTY_STOP + 1


def test_discover_zero_row_tripwire(monkeypatch, capsys):
    """Finding 3: a grep-able WARN fires when discovery finds 0 reports."""
    monkeypatch.setattr(caav, "DELAY", 0)
    monkeypatch.setattr(caav, "iter_listing_urls", lambda *a, **k: [caav.INDEX_URL])
    # listing has NO rows and id-window is empty (no seed ids)
    conn = _conn()
    client = _EnumClient([], {})
    assert pipeline.discover(conn, client) == 0
    err = capsys.readouterr().err
    assert "[caav WARN] discovery yielded 0 report rows" in err


# ── fetch ─────────────────────────────────────────────────────────────────

def _seed_new(conn, case_id, pdf_url):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO caav_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_downloads_and_advances(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "VN-8650-2023-04-05", "https://imgcaa.minhvujsc.com/a.pdf")
    calls = []
    def _dl(client, url, dest):
        calls.append((url, dest)); open(dest, "wb").write(b"%PDF")
    monkeypatch.setattr(caav, "download", _dl)
    monkeypatch.setattr(caav, "DELAY", 0)

    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    row = conn.execute("SELECT status, pdf_path FROM caav_reports WHERE case_id='VN-8650-2023-04-05'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert os.path.exists(row["pdf_path"])
    assert len(calls) == 1


def test_fetch_no_pdf_url_advances_null_path(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "VN-x", None)
    calls = []
    monkeypatch.setattr(caav, "download", lambda *a: calls.append(a))
    monkeypatch.setattr(caav, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert not calls
    row = conn.execute("SELECT status, pdf_path FROM caav_reports WHERE case_id='VN-x'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_failure_keeps_new(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "VN-a", "https://imgcaa.minhvujsc.com/a.pdf")
    monkeypatch.setattr(caav, "download",
                        lambda c, u, d: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(caav, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 1
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-a'").fetchone()["status"] == db.STATUS_NEW


def test_fetch_isolates_per_row_errors(monkeypatch, tmp_path):
    conn = _conn()
    _seed_new(conn, "VN-a", "https://imgcaa.minhvujsc.com/a.pdf")
    _seed_new(conn, "VN-b", "https://imgcaa.minhvujsc.com/b.pdf")
    def _sel(c, u, d):
        if "a.pdf" in u:
            raise RuntimeError("403")
        open(d, "wb").write(b"%PDF")
    monkeypatch.setattr(caav, "download", _sel)
    monkeypatch.setattr(caav, "DELAY", 0)
    assert pipeline.fetch(conn, None, str(tmp_path)) == 2
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-a'").fetchone()["status"] == db.STATUS_NEW
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-b'").fetchone()["status"] == db.STATUS_FETCHED


# ── parse ───────────────────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path=None):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO caav_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, ts, ts),
    )
    conn.commit()


def test_parse_long_narrative_pdf_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "VN-a", "a.pdf")
    long_text = "X" * MIN_NARRATIVE
    monkeypatch.setattr(pipeline, "extract_text", lambda p: long_text)
    assert pipeline.parse(conn) == 1
    row = conn.execute("SELECT source_tier, status, narrative_text FROM caav_reports WHERE case_id='VN-a'").fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_short_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "VN-a", "a.pdf")
    txt = "Y" * 550  # >= floor 500, < MIN_NARRATIVE 600
    monkeypatch.setattr(pipeline, "extract_text", lambda p: txt)
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM caav_reports WHERE case_id='VN-a'").fetchone()["source_tier"] == "short"


def test_parse_scanned_tier(monkeypatch):
    """PDF exists but text is tiny → scanned."""
    conn = _conn()
    _seed_fetched(conn, "VN-a", "a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "only a caption, 30 chars")
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM caav_reports WHERE case_id='VN-a'").fetchone()["source_tier"] == "scanned"


def test_parse_no_pdf_path_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "VN-a", None)
    calls = []
    monkeypatch.setattr(pipeline, "extract_text", lambda p: calls.append(p) or "X" * 1000)
    pipeline.parse(conn)
    assert not calls
    assert conn.execute("SELECT source_tier FROM caav_reports WHERE case_id='VN-a'").fetchone()["source_tier"] == "none"


def test_parse_empty_extraction_none_tier(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "VN-a", "a.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "")
    pipeline.parse(conn)
    assert conn.execute("SELECT source_tier FROM caav_reports WHERE case_id='VN-a'").fetchone()["source_tier"] == "none"


# ── build ───────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, *, aircraft=None, registration=None, location=None,
                 date=None, narrative="", event_class=None, operator=None,
                 pdf_url=None, report_url=None, tier="pdf"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO caav_reports (case_id, aircraft, registration, location, "
        "date_of_occurrence, narrative_text, event_class, operator, pdf_url, "
        "report_url, source_tier, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (case_id, aircraft, registration, location, date, narrative, event_class,
         operator, pdf_url, report_url, tier, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()


def test_build_creates_accident_row():
    conn = _conn()
    narr = "N" * 2000
    _seed_parsed(conn, "VN-8650-2023-04-05", aircraft="Bell 505",
                 registration="VN-8650", location="Ha Long Bay", date="2023-04-05",
                 narrative=narr, event_class="Serious incident",
                 pdf_url="https://imgcaa.minhvujsc.com/a.pdf")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM caav_accidents WHERE case_id='VN-8650-2023-04-05'").fetchone()
    assert acc["country"] == "VN"
    assert acc["event_date"] == "2023-04-05"
    assert acc["aircraft"] == "Bell 505"
    assert acc["registration"] == "VN-8650"
    assert acc["narrative_text"] == narr
    assert acc["source_url"] == "https://imgcaa.minhvujsc.com/a.pdf"
    assert acc["site_slug"].startswith("crash-")
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-8650-2023-04-05'").fetchone()["status"] == db.STATUS_BUILT


def test_build_source_url_falls_back_to_report_url():
    conn = _conn()
    _seed_parsed(conn, "VN-a", narrative="N" * 200, event_class="Serious incident",
                 pdf_url=None, report_url="https://english.caa.gov.vn/doc-detail/r1.htm")
    pipeline.build(conn)
    acc = conn.execute("SELECT source_url FROM caav_accidents WHERE case_id='VN-a'").fetchone()
    assert acc["source_url"] == "https://english.caa.gov.vn/doc-detail/r1.htm"


def test_build_skips_empty_narrative():
    conn = _conn()
    _seed_parsed(conn, "VN-a", narrative="", event_class="Serious incident",
                 pdf_url="https://imgcaa.minhvujsc.com/a.pdf")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-a'").fetchone()["status"] == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM caav_accidents").fetchone()[0] == 0


def test_build_skips_below_floor():
    conn = _conn()
    _seed_parsed(conn, "VN-a", narrative="X" * 79, event_class="Serious incident")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-a'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_skips_scanned_even_with_text():
    """A 'scanned' tier row is skipped even if it has > floor chars."""
    conn = _conn()
    _seed_parsed(conn, "VN-a", narrative="S" * 400, event_class="Serious incident", tier="scanned")
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-a'").fetchone()["status"] == db.STATUS_SKIPPED


def test_build_country_is_vn():
    conn = _conn()
    _seed_parsed(conn, "VN-a", narrative="N" * 200, event_class="Serious incident")
    pipeline.build(conn)
    assert conn.execute("SELECT country FROM caav_accidents WHERE case_id='VN-a'").fetchone()["country"] == "VN"


def test_build_mixed_rows():
    conn = _conn()
    long_narr = "Z" * 800
    _seed_parsed(conn, "VN-1", aircraft="A321", narrative=long_narr, event_class="Serious incident")
    _seed_parsed(conn, "VN-2", aircraft="B737", narrative=long_narr, event_class="Serious incident")
    _seed_parsed(conn, "VN-3", aircraft="C172", narrative="", event_class="Serious incident")
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM caav_accidents").fetchone()[0] == 2
    assert conn.execute("SELECT status FROM caav_reports WHERE case_id='VN-3'").fetchone()["status"] == db.STATUS_SKIPPED
