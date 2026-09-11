import os
import pytest

from nbaai_ingest import db, nbaai, pipeline
from nbaai_ingest.pdf import MIN_NARRATIVE

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover_inserts_from_fixtures(make_client):
    sm = _read("enquiry_sitemap.xml")
    # Restrict the sitemap to the two URLs we have fixture pages for
    u1 = "https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/"
    u2 = "https://nbaai.gov.ua/enquiry/zaversheno-rozsliduvannya-katastrofy-litaka-l-410-ur-two/"
    mini_sm = (
        f"<urlset><url><loc>{u1}</loc></url><url><loc>{u2}</loc></url></urlset>"
    )
    routes = {
        nbaai.SITEMAP_URL: lambda _u: _Resp(mini_sm.encode()),
        u1: lambda _u: _Resp(_read("detail_html_only.html").encode()),
        u2: lambda _u: _Resp(_read("detail_with_pdf.html").encode()),
    }
    conn = _conn()
    n = pipeline.discover(conn, make_client(routes))
    assert n == 2
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM nbaai_reports")}
    assert "NBAAI-UR-KTB-2019-10-21" in rows
    # HTML body captured at discover time (no PDF)
    assert rows["NBAAI-UR-KTB-2019-10-21"]["pdf_url"] is None
    assert len(rows["NBAAI-UR-KTB-2019-10-21"]["narrative_text"]) > 80
    # PDF report row has pdf_url set
    l410 = [r for cid, r in rows.items() if r["registration"] == "UR-TWO"][0]
    assert l410["pdf_url"].endswith("l-410_ur-two.pdf")


def test_discover_idempotent(make_client):
    u1 = "https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/"
    mini_sm = f"<urlset><url><loc>{u1}</loc></url></urlset>"
    routes = {
        nbaai.SITEMAP_URL: lambda _u: _Resp(mini_sm.encode()),
        u1: lambda _u: _Resp(_read("detail_html_only.html").encode()),
    }
    conn = _conn()
    client = make_client(routes)
    assert pipeline.discover(conn, client) == 1
    assert pipeline.discover(conn, client) == 0  # second pass inserts nothing


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


# ── parse: HTML-only path ────────────────────────────────────────────────────

def test_parse_html_only_tier_html():
    conn = _conn()
    body = "Катастрофа вертольота R-44. " * 10  # > 80 chars, clean Ukrainian
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, pdf_path, narrative_text, status) "
        "VALUES ('C1', NULL, ?, 'fetched')",
        (body,),
    )
    conn.commit()
    pipeline.parse(conn)
    r = conn.execute("SELECT narrative_text, source_tier FROM nbaai_reports WHERE case_id='C1'").fetchone()
    assert r["source_tier"] == "html"
    assert r["narrative_text"].startswith("Катастрофа")


# ── parse: clean PDF text-layer path ─────────────────────────────────────────

def test_parse_clean_pdf_tier_pdf(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, pdf_path, narrative_text, status) "
        "VALUES ('C2', '/tmp/x.pdf', '', 'fetched')"
    )
    conn.commit()
    clean = ("Остаточний звіт про розслідування серйозного інциденту. "
             "Екіпаж, аеродром, реєстрація повітряного судна. ") * 20  # >600, usable
    monkeypatch.setattr(pipeline, "extract_text", lambda p: clean)
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, l: pytest.fail("OCR must NOT run on clean PDF"))
    pipeline.parse(conn)
    r = conn.execute("SELECT source_tier FROM nbaai_reports WHERE case_id='C2'").fetchone()
    assert r["source_tier"] == "pdf"


# ── parse: MOJIBAKE text-layer TRIGGERS OCR (the defining trait) ─────────────

def test_parse_mojibake_triggers_ocr(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, pdf_path, narrative_text, status) "
        "VALUES ('C3', '/tmp/moji.pdf', '', 'fetched')"
    )
    conn.commit()

    # Non-Unicode embedded-font output: long enough to clear a raw char gate,
    # but it contains NO recognizable marker words -> is_usable_text() == False.
    mojibake = "PEIIYEJII4KA CPBI4JA HECPEhA Y CAOEPAhAJY 3BJIITP" * 30
    assert len(mojibake) > MIN_NARRATIVE

    recovered = ("РОЗСЛІДУВАННЯ КАТАСТРОФИ ЛІТАКА L-410. "
                 "Екіпаж загинув. Аеродром призначення. ") * 5  # usable Ukrainian
    ocr_calls = []

    def fake_ocr(path, lang):
        ocr_calls.append((path, lang))
        return recovered

    monkeypatch.setattr(pipeline, "extract_text", lambda p: mojibake)
    monkeypatch.setattr(pipeline, "ocr_extract", fake_ocr)

    pipeline.parse(conn)

    # OCR was invoked with the Ukrainian+Russian language pack
    assert ocr_calls == [("/tmp/moji.pdf", "ukr+rus")]
    r = conn.execute("SELECT narrative_text, source_tier FROM nbaai_reports WHERE case_id='C3'").fetchone()
    assert r["source_tier"] == "ocr"
    assert "РОЗСЛІДУВАННЯ" in r["narrative_text"]
    # the garbage mojibake must NOT have been kept
    assert "PEIIYEJII4KA" not in r["narrative_text"]


# ── parse: scanned PDF (empty text-layer) triggers OCR ───────────────────────

def test_parse_scanned_empty_triggers_ocr(monkeypatch):
    conn = _conn()
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, pdf_path, narrative_text, status) "
        "VALUES ('C4', '/tmp/scan.pdf', '', 'fetched')"
    )
    conn.commit()
    recovered = "Звіт про авіаційну подію. Пілот. Розслідування. Аеродром. " * 4
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "\x0c   ")  # scanned
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, l: recovered)
    pipeline.parse(conn)
    r = conn.execute("SELECT source_tier FROM nbaai_reports WHERE case_id='C4'").fetchone()
    assert r["source_tier"] == "ocr"


# ── parse: OCR fails -> fall back to HTML body ───────────────────────────────

def test_parse_ocr_fails_falls_back_to_html(monkeypatch):
    conn = _conn()
    html_body = "Катастрофа літака в районі аеродрому. Розслідування комісії НБРТ. " * 4
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, pdf_path, narrative_text, status) "
        "VALUES ('C5', '/tmp/bad.pdf', ?, 'fetched')",
        (html_body,),
    )
    conn.commit()
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "garble" * 5)  # mojibake-ish, no markers
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, l: "")  # OCR yields nothing
    pipeline.parse(conn)
    r = conn.execute("SELECT narrative_text, source_tier FROM nbaai_reports WHERE case_id='C5'").fetchone()
    assert r["source_tier"] == "html"
    assert "Катастрофа" in r["narrative_text"]


# ── build ────────────────────────────────────────────────────────────────────

def test_build_projects_and_skips_thin():
    conn = _conn()
    good = "Розслідування катастрофи літака. " * 5
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, registration, date_of_occurrence, "
        "aircraft, narrative_text, source_tier, report_url, status) "
        "VALUES ('NBAAI-UR-KTB-2019-10-21','UR-KTB','2019-10-21','R-44',?,'html',"
        "'https://nbaai.gov.ua/enquiry/x/','parsed')",
        (good,),
    )
    conn.execute(
        "INSERT INTO nbaai_reports (case_id, narrative_text, source_tier, status) "
        "VALUES ('NBAAI-THIN','too short','none','parsed')"
    )
    conn.commit()
    built = pipeline.build(conn)
    assert built == 1
    acc = conn.execute("SELECT * FROM nbaai_accidents WHERE case_id='NBAAI-UR-KTB-2019-10-21'").fetchone()
    assert acc["country"] == "UA"
    assert acc["registration"] == "UR-KTB"
    assert acc["event_date"] == "2019-10-21"
    assert acc["site_slug"].startswith("crash-")
    thin = conn.execute("SELECT status FROM nbaai_reports WHERE case_id='NBAAI-THIN'").fetchone()
    assert thin["status"] == "skipped"
