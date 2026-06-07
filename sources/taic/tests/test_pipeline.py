"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pathlib

import pytest

from taic_ingest import db, pipeline, taic

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    """Maps (url or page) → response text; records requested URLs."""

    def __init__(self, pages=None, urls=None):
        self.pages = pages or {}   # page number → listing html
        self.urls = urls or {}     # absolute url → html/bytes
        self.requested = []

    def get(self, url, params=None):
        if params is not None and "page" in params:
            self.requested.append(f"page={params['page']}")
            return FakeResp(text=self.pages.get(params["page"], "<html></html>"))
        self.requested.append(url)
        val = self.urls.get(url, "")
        if isinstance(val, bytes):
            return FakeResp(content=val)
        return FakeResp(text=val)


def _mk_card(case_id, pill="Published", publish="2020-05-01"):
    pub_val = (
        f'<time datetime="{publish}T12:00:00Z">x</time>'
        if publish
        else "Not yet published"
    )
    return f'''
    <div class="card card-type--inquiry" data-relevance="1">
      <span class="card__incident"> {case_id} </span>
      <div class="card__header"><h3 class="card__title">
        <a href="/inquiry/{case_id.lower()}" hreflang="en">Test title {case_id}</a>
      </h3></div>
      <div class="card__body">
        <p class="card__text">Summary text for {case_id}.</p>
        <span class="card__date"><span class="date-type">Incident date:</span>
          <span class="date-value"><time datetime="2019-03-02T12:00:00Z">2 Mar 2019</time></span></span>
        <span class="card__date"><span class="date-type">Publish date:</span>
          <span class="date-value">{pub_val}</span></span>
      </div>
      <div class="card__footer"><span class="card__pill">{pill}</span></div>
    </div>'''


def _listing(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(taic, "DELAY", 0)


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_aviation_only_and_stops_on_empty(conn):
    pages = {
        0: _listing(_mk_card("AO-2019-001"), _mk_card("MO-2019-201"),
                    _mk_card("RO-2019-101")),
        1: _listing(_mk_card("AO-2018-002")),
        2: _listing(),  # empty → stop
        3: _listing(_mk_card("AO-2000-009")),  # must never be reached
    }
    client = FakeClient(pages=pages)
    n = pipeline.discover(conn, client)
    assert n == 2
    ids = [r["case_id"] for r in conn.execute(
        "SELECT case_id FROM taic_reports ORDER BY case_id")]
    assert ids == ["AO-2018-002", "AO-2019-001"]
    assert "page=3" not in client.requested


def test_discover_idempotent(conn):
    pages = {0: _listing(_mk_card("AO-2019-001")), 1: _listing()}
    assert pipeline.discover(conn, FakeClient(pages=pages)) == 1
    assert pipeline.discover(conn, FakeClient(pages=pages)) == 0


def test_discover_resets_row_when_pill_flips_to_published(conn):
    in_prog = {0: _listing(_mk_card("AO-2024-003", pill="In progress",
                                    publish=None)),
               1: _listing()}
    pipeline.discover(conn, FakeClient(pages=in_prog))
    # fetch skips it (not Published) — stays new
    assert pipeline.fetch(conn, FakeClient(), pdf_dir="/tmp") == 0
    # simulate it being parsed already (pretend an old cycle)
    conn.execute("UPDATE taic_reports SET status='skipped' WHERE case_id='AO-2024-003'")
    conn.commit()
    published = {0: _listing(_mk_card("AO-2024-003", pill="Published")),
                 1: _listing()}
    pipeline.discover(conn, FakeClient(pages=published))
    row = conn.execute(
        "SELECT status, pill, publish_date FROM taic_reports WHERE case_id='AO-2024-003'"
    ).fetchone()
    assert row["status"] == "new"
    assert row["pill"] == "Published"
    assert row["publish_date"] == "2020-05-01"


def test_discover_max_pages_cap(conn):
    pages = {0: _listing(_mk_card("AO-2019-001")),
             1: _listing(_mk_card("AO-2018-002"))}
    client = FakeClient(pages=pages)
    pipeline.discover(conn, client, max_pages=1)
    assert "page=1" not in client.requested


# ── fetch ─────────────────────────────────────────────────────────────────────

def _seed(conn, case_id="AO-2018-006", pill="Published"):
    conn.execute(
        "INSERT INTO taic_reports (case_id, inquiry_url, title, pill, status, "
        "discovered_at, updated_at) VALUES (?,?,?,?, 'new', 0, 0)",
        (case_id, f"https://taic.org.nz/inquiry/{case_id.lower()}",
         f"t {case_id}", pill),
    )
    conn.commit()


def test_fetch_html_tier(conn, inquiry_rich_html, tmp_path):
    _seed(conn)
    client = FakeClient(
        urls={"https://taic.org.nz/inquiry/ao-2018-006": inquiry_rich_html})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM taic_reports WHERE case_id='AO-2018-006'").fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "html"
    assert len(row["narrative_text"]) > 50_000
    assert row["registration"] == "ZK-HTB"
    assert row["date_of_occurrence"] == "2018-07-21"
    # no PDF download needed for rich HTML pages
    assert row["pdf_path"] is None


def test_fetch_pdf_fallback_scanned(conn, inquiry_old_html, tmp_path, monkeypatch):
    _seed(conn, case_id="AO-1995-009")
    pdf_url = "https://taic.org.nz/sites/default/files/inquiry/documents/95-009.pdf"
    client = FakeClient(urls={
        "https://taic.org.nz/inquiry/ao-1995-009": inquiry_old_html,
        pdf_url: b"%PDF-1.4 fake scan",
    })
    # pdftotext on the fake scan yields nothing
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM taic_reports WHERE case_id='AO-1995-009'").fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "scanned"
    assert row["pdf_url"] == pdf_url
    assert (tmp_path / "AO-1995-009.pdf").exists()


def test_fetch_pdf_fallback_text_layer(conn, inquiry_old_html, tmp_path, monkeypatch):
    _seed(conn, case_id="AO-1995-009")
    pdf_url = "https://taic.org.nz/sites/default/files/inquiry/documents/95-009.pdf"
    client = FakeClient(urls={
        "https://taic.org.nz/inquiry/ao-1995-009": inquiry_old_html,
        pdf_url: b"%PDF-1.4 fake",
    })
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "X" * 5000)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM taic_reports WHERE case_id='AO-1995-009'").fetchone()
    assert row["source_tier"] == "pdf"
    assert len(row["narrative_text"]) == 5000


def test_fetch_skips_in_progress(conn):
    _seed(conn, case_id="AO-2025-004", pill="In progress")
    client = FakeClient()
    assert pipeline.fetch(conn, client, pdf_dir="/tmp") == 0
    assert client.requested == []
    row = conn.execute("SELECT status FROM taic_reports WHERE case_id='AO-2025-004'").fetchone()
    assert row["status"] == "new"


def test_fetch_failure_stays_new(conn, tmp_path):
    _seed(conn)

    class Boom(FakeClient):
        def get(self, url, params=None):
            raise RuntimeError("boom")

    pipeline.fetch(conn, Boom(), pdf_dir=str(tmp_path))
    row = conn.execute("SELECT status FROM taic_reports WHERE case_id='AO-2018-006'").fetchone()
    assert row["status"] == "new"


# ── build ─────────────────────────────────────────────────────────────────────

def _seed_parsed(conn, case_id, narrative, tier="html", **cols):
    conn.execute(
        "INSERT INTO taic_reports (case_id, inquiry_url, title, pill, status, "
        "narrative_text, source_tier, aircraft, registration, operator, "
        "location, date_of_occurrence, discovered_at, updated_at) "
        "VALUES (?,?,?,?, 'parsed', ?,?,?,?,?,?,?, 0, 0)",
        (case_id, f"https://taic.org.nz/inquiry/{case_id.lower()}",
         f"Title {case_id}", "Published", narrative, tier,
         cols.get("aircraft"), cols.get("registration"), cols.get("operator"),
         cols.get("location"), cols.get("date")),
    )
    conn.commit()


def test_build_promotes_substantive_rows(conn):
    _seed_parsed(conn, "AO-2018-006", "N" * 5000, aircraft="Robinson R44",
                 registration="ZK-HTB", location="Lake Wanaka",
                 date="2018-07-21")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM taic_accidents WHERE case_id='AO-2018-006'").fetchone()
    assert acc["country"] == "NZ"
    assert acc["event_date"] == "2018-07-21"
    assert acc["registration"] == "ZK-HTB"
    assert acc["site_slug"].startswith("crash-robinson-r44-zk-htb")
    assert acc["source_url"] == "https://taic.org.nz/inquiry/ao-2018-006"
    row = conn.execute("SELECT status FROM taic_reports WHERE case_id='AO-2018-006'").fetchone()
    assert row["status"] == "built"


def test_build_skips_short_narratives(conn):
    _seed_parsed(conn, "AO-1995-009", "too short", tier="scanned")
    assert pipeline.build(conn) == 0
    row = conn.execute("SELECT status FROM taic_reports WHERE case_id='AO-1995-009'").fetchone()
    assert row["status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) c FROM taic_accidents").fetchone()["c"] == 0


def test_build_idempotent_reinsert(conn):
    _seed_parsed(conn, "AO-2018-006", "N" * 5000)
    pipeline.build(conn)
    # re-parse → re-build replaces, no dup
    conn.execute("UPDATE taic_reports SET status='parsed' WHERE case_id='AO-2018-006'")
    conn.commit()
    assert pipeline.build(conn) == 1
    assert conn.execute("SELECT COUNT(*) c FROM taic_accidents").fetchone()["c"] == 1
