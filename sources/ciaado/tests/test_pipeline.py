"""Pipeline tests using saved fixtures + a fake httpx client."""
import os

from ciaado_ingest import ciaado, db, pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_discover_inserts_rows(make_client):
    """discover walks top cats → subcats; rows land in ciaado_reports."""
    top = _fixture_bytes("ciaado_top_informes.html")
    y2016 = _fixture_bytes("ciaado_year_2016.html")
    y2019 = _fixture_bytes("ciaado_year_2019.html")

    routes = {}
    # the three top categories — only 19-informes returns the rich fixture,
    # the others return an empty page (no subcats)
    routes[ciaado.BASE + "/index.php/informesf/category/19-informes"] = \
        _resp(top)
    routes[ciaado.BASE + "/index.php/informesf/category/29-informes-preliminares"] = \
        _resp(b"<html></html>")
    routes[ciaado.BASE + "/index.php/informesf/category/40-declaraciones-provisionales"] = \
        _resp(b"<html></html>")
    # year subcats discovered from the top fixture: serve fixtures for two,
    # empty for the rest
    for u in ciaado.iter_subcategory_urls(top.decode("utf-8", "replace")):
        if u.endswith("/36-2016"):
            routes[u] = _resp(y2016)
        elif u.endswith("/20-2019"):
            routes[u] = _resp(y2019)
        else:
            routes[u] = _resp(b"<html></html>")

    client = make_client(routes)
    conn = _conn()
    n = pipeline.discover(conn, client)
    assert n >= 18, f"expected >=18 inserted, got {n}"

    cnt = conn.execute("SELECT COUNT(*) c FROM ciaado_reports").fetchone()["c"]
    assert cnt == n
    # spot-check a known row
    row = conn.execute(
        "SELECT * FROM ciaado_reports WHERE case_id='CIAA-101-2019'"
    ).fetchone()
    assert row is not None
    assert row["registration"] == "HI-878"
    assert "?download=" in row["pdf_url"]
    assert row["status"] == db.STATUS_NEW
    assert row["lang"] == "es"


def test_discover_idempotent(make_client):
    top = _fixture_bytes("ciaado_top_informes.html")
    y2016 = _fixture_bytes("ciaado_year_2016.html")
    routes = {
        ciaado.BASE + "/index.php/informesf/category/19-informes": _resp(top),
        ciaado.BASE + "/index.php/informesf/category/29-informes-preliminares": _resp(b"<html></html>"),
        ciaado.BASE + "/index.php/informesf/category/40-declaraciones-provisionales": _resp(b"<html></html>"),
    }
    for u in ciaado.iter_subcategory_urls(top.decode("utf-8", "replace")):
        routes[u] = _resp(y2016) if u.endswith("/36-2016") else _resp(b"<html></html>")

    conn = _conn()
    first = pipeline.discover(conn, make_client(routes))
    second = pipeline.discover(conn, make_client(routes))
    assert first > 0
    assert second == 0  # nothing new on the second pass


def test_parse_tiers(tmp_path):
    """parse classifies pdf / short / scanned by extracted text length."""
    conn = _conn()
    ts = db.now_ms()
    # one long, one short, one missing-pdf (scanned)
    conn.execute(
        "INSERT INTO ciaado_reports (case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES ('CIAA-001-2008','/a.pdf',?,?,?)",
        (db.STATUS_FETCHED, ts, ts),
    )
    conn.execute(
        "INSERT INTO ciaado_reports (case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES ('CIAA-002-2008','/b.pdf',?,?,?)",
        (db.STATUS_FETCHED, ts, ts),
    )
    conn.execute(
        "INSERT INTO ciaado_reports (case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES ('CIAA-003-2008',NULL,?,?,?)",
        (db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()

    texts = {"/a.pdf": "x" * 800, "/b.pdf": "corto"}
    pipeline.extract_text = lambda p: texts.get(p, "")  # monkeypatch module ref

    pipeline.parse(conn)

    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM ciaado_reports")}
    assert rows["CIAA-001-2008"]["source_tier"] == "pdf"
    assert rows["CIAA-002-2008"]["source_tier"] == "short"
    assert rows["CIAA-003-2008"]["source_tier"] == "scanned"
    assert all(r["status"] == db.STATUS_PARSED for r in rows.values())


def test_build_emits_and_skips():
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ciaado_reports "
        "(case_id, registration, narrative_text, pdf_url, event_class, status, discovered_at, updated_at) "
        "VALUES ('CIAA-101-2019','HI-878',?, 'http://x?download=1','Final report',?,?,?)",
        ("n" * 500, db.STATUS_PARSED, ts, ts),
    )
    conn.execute(
        "INSERT INTO ciaado_reports "
        "(case_id, narrative_text, status, discovered_at, updated_at) "
        "VALUES ('CIAA-002-2008','too short',?,?,?)",
        (db.STATUS_PARSED, ts, ts),
    )
    conn.commit()

    built = pipeline.build(conn)
    assert built == 1

    acc = conn.execute("SELECT * FROM ciaado_accidents").fetchall()
    assert len(acc) == 1
    a = acc[0]
    assert a["case_id"] == "CIAA-101-2019"
    assert a["country"] == "DO"
    assert a["report_type"] == "Final report"
    assert a["site_slug"].startswith("crash-")
    assert a["source_url"] == "http://x?download=1"

    # the short one was skipped
    short = conn.execute(
        "SELECT status FROM ciaado_reports WHERE case_id='CIAA-002-2008'"
    ).fetchone()
    assert short["status"] == db.STATUS_SKIPPED


# ── helper ────────────────────────────────────────────────────────────────

def _resp(content_bytes):
    from tests.conftest import FakeResp
    return FakeResp(content=content_bytes, status_code=200)
