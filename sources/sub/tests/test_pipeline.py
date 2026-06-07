"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from sub_ingest import sub, pipeline

B = "https://www.bmimi.gv.at"

# Hub → 2 categories: one YEAR-based (motorflugzeuge, 1 year w/ 2 reports) and
# one FLAT (heissluftballons, 1 report directly on the category page).
_HUB = (
    '<main id="content">'
    f'<a href="/sub/berichte/luftfahrt/motorflugzeuge.html">M</a>'
    f'<a href="/sub/berichte/luftfahrt/heissluftballons.html">H</a>'
    '</main>'
)

_CAT_MOTOR = (
    '<main id="content">'
    '<a href="/sub/berichte/luftfahrt/motorflugzeuge/2024.html" class="card-link">2024</a>'
    '</main>'
)

_YEAR_2024 = (
    '<main id="content">'
    '<a href="/sub/berichte/luftfahrt/motorflugzeuge/2024/0330_cirrus_85305.html" class="card-link">r1</a>'
    '<a href="/sub/berichte/luftfahrt/motorflugzeuge/2024/0525_tr182_85321.html" class="card-link">r2</a>'
    '</main>'
)

# FLAT category page: report links directly, NO year links.
_CAT_BALL = (
    '<main id="content">'
    '<a href="/sub/berichte/luftfahrt/heissluftballons/20221112_g60_85297.html" class="card-link">b1</a>'
    '</main>'
)


def _report(date, aircraft, gz, loc, kind, pdf):
    sub_html = (f'<span class="subtitle"><abbr title="Geschäftszahl">GZ</abbr>&#xa0;{gz}</span>'
                if gz else '<span class="subtitle"></span>')
    info = (
        '<div class="infobox"><p>'
        f'<a href="{pdf}" class="file" download="">'
        '<span class="icon icon-datei_pdf"></span>'
        f'{kind}&#xa0;„{aircraft}“ <span class="fileinfo">(PDF, 5 MB)</span></a>'
        '<br />erstellt am 1. Jänner 2025</p></div>'
    ) if pdf else '<div class="infobox"><p>kein Bericht</p></div>'
    return (
        '<main id="content">'
        f'<time class="datetime" datetime="{date}">x</time>'
        f'<span class="title">{aircraft}&#xa0;</span></div>'
        f'{sub_html}'
        f'<p class="abstract">{loc}</p>'
        '<p>Ein ausreichend langer deutscher Zusammenfassungstext. ' + ('X' * 400) + '</p>'
        f'{info}'
        '</main>'
    )


_R1 = f"{B}/sub/berichte/luftfahrt/motorflugzeuge/2024/0330_cirrus_85305.html"
_R2 = f"{B}/sub/berichte/luftfahrt/motorflugzeuge/2024/0525_tr182_85321.html"
_B1 = f"{B}/sub/berichte/luftfahrt/heissluftballons/20221112_g60_85297.html"

_PDF1 = f"{B}/dam/jcr:p1/cirrus.pdf"
_PDF2 = f"{B}/dam/jcr:p2/tr182.pdf"
_PDFB = f"{B}/dam/jcr:pb/g60.pdf"

_PAGES = {
    f"{B}/sub/berichte/luftfahrt.html": _HUB,
    f"{B}/sub/berichte/luftfahrt/motorflugzeuge.html": _CAT_MOTOR,
    f"{B}/sub/berichte/luftfahrt/motorflugzeuge/2024.html": _YEAR_2024,
    f"{B}/sub/berichte/luftfahrt/heissluftballons.html": _CAT_BALL,
    _R1: _report("2024-03-30", "Cirrus SR20", "2025-0.1", "bei Wien",
                 "Abschlussbericht", _PDF1),
    _R2: _report("2024-05-25", "Cessna TR182", None, "bei Graz",
                 "Untersuchungsbericht", _PDF2),
    _B1: _report("2022-11-12", "Schroeder G60", "2024-0.9", "bei Linz",
                 "Vereinfachter Untersuchungsbericht", _PDFB),
}


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pdfs=None, fail_pdf=None):
        self.pdfs = pdfs if pdfs is not None else {
            _PDF1: b"%PDF1", _PDF2: b"%PDF2", _PDFB: b"%PDFB"}
        self.fail_pdf = fail_pdf or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in _PAGES:
            return FakeResp(text=_PAGES[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sub, "DELAY", 0)


def test_discover_year_and_flat(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 3  # 2 year-based reports + 1 flat report
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM sub_reports")}
    assert set(rows) == {
        "motorflugzeuge--2024--0330_cirrus_85305",
        "motorflugzeuge--2024--0525_tr182_85321",
        "heissluftballons--20221112_g60_85297",
    }
    r1 = rows["motorflugzeuge--2024--0330_cirrus_85305"]
    assert r1["category"] == "motorflugzeuge"
    assert r1["year"] == "2024"
    assert r1["date_of_occurrence"] == "2024-03-30"
    assert r1["aircraft"] == "Cirrus SR20"
    assert r1["report_kind"] == "Abschlussbericht"
    assert r1["pdf_url"] == _PDF1
    assert r1["summary_text"] and len(r1["summary_text"]) > 300
    flat = rows["heissluftballons--20221112_g60_85297"]
    assert flat["category"] == "heissluftballons"
    assert flat["year"] is None
    assert flat["report_kind"] == "Vereinfachter Untersuchungsbericht"


def test_discover_visits_hub_categories_and_reports(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    req = client.requested
    assert f"{B}/sub/berichte/luftfahrt.html" in req
    assert f"{B}/sub/berichte/luftfahrt/motorflugzeuge.html" in req
    assert f"{B}/sub/berichte/luftfahrt/motorflugzeuge/2024.html" in req
    assert f"{B}/sub/berichte/luftfahrt/heissluftballons.html" in req
    # flat category page is NOT requested as a year page (no /YYYY.html)
    assert f"{B}/sub/berichte/luftfahrt/heissluftballons/2022.html" not in req
    assert _R1 in req and _B1 in req


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier_and_registration(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Bericht " + "N" * 9000 + " Luftfahrzeug OE-DXY im Flug.")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM sub_reports")}
    r1 = rows["motorflugzeuge--2024--0330_cirrus_85305"]
    assert r1["source_tier"] == "pdf"
    assert r1["status"] == "parsed"
    assert r1["registration"] == "OE-DXY"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    r = conn.execute(
        "SELECT * FROM sub_reports WHERE case_id='motorflugzeuge--2024--0330_cirrus_85305'"
    ).fetchone()
    assert r["source_tier"] == "scanned"
    assert r["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF1})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    r = conn.execute(
        "SELECT * FROM sub_reports WHERE case_id='motorflugzeuge--2024--0330_cirrus_85305'"
    ).fetchone()
    assert r["status"] == "new"  # retried next cycle


def test_build_floor_pdf(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 3
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM sub_accidents")}
    assert len(acc) == 3
    a = acc["motorflugzeuge--2024--0330_cirrus_85305"]
    assert a["country"] == "AT"
    assert a["lang"] == "de"
    assert a["report_type"] == "Abschlussbericht"
    assert a["event_date"] == "2024-03-30"
    assert a["source_url"] == _R1
    assert len(a["narrative_text"]) >= 9000  # PDF text, not the summary


def test_build_summary_fallback_when_pdf_scanned(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    # PDF has no text layer → scanned; build must fall back to the HTML summary.
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 3
    a = conn.execute(
        "SELECT narrative_text FROM sub_accidents "
        "WHERE case_id='motorflugzeuge--2024--0330_cirrus_85305'"
    ).fetchone()
    # Narrative came from the stored HTML summary paragraphs.
    assert a["narrative_text"].startswith("Ein ausreichend langer deutscher")


def test_build_skips_when_no_text_and_no_summary(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    # Wipe both the PDF text and the summary → must be skipped, not built.
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    conn.execute("UPDATE sub_reports SET summary_text='' ")
    conn.commit()
    assert pipeline.build(conn) == 0
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM sub_reports WHERE status='skipped'").fetchone()["c"]
    assert skipped == 3


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE sub_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM sub_accidents").fetchone()["c"] == 3
