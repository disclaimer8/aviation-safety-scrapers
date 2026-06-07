"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from jst_ingest import jst, pipeline


class FakeResp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """
    Serves the manifest, paginated event pages (modo=2), and PDF bytes.
    pages = list-of-lists of raw event dicts (one inner list per page).
    """
    def __init__(self, pages, manifest, pdfs=None):
        self.pages = pages
        self.manifest = manifest
        self.pdfs = pdfs if pdfs is not None else {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == jst.MANIFEST_URL:
            return FakeResp(payload=self.manifest)
        if url.startswith(jst.EVENTS_BASE):
            # extract pagina=N
            import re
            n = int(re.search(r"pagina=(\d+)", url).group(1))
            events = self.pages[n - 1] if n - 1 < len(self.pages) else []
            return FakeResp(payload={"expedientes": events})
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        raise RuntimeError("404")


def _event(nro, matricula="LV-ABC", fatal=0, fecha="2020-01-01"):
    return {
        "nro_expediente": nro, "fecha": fecha, "estado": "Finalizada",
        "lugar": "Aeropuerto Test (Buenos Aires)", "reseña": "R" * 80,
        "vehiculos": [{
            "marca": "CESSNA", "modelo": "C-172", "matricula": matricula,
            "operacion": "Aviación General", "suceso": "Accidente",
            "victimas_fatales": fatal,
        }],
    }


# one ISO+IB event, one IP-only event, one doc-less stub
_EVENTS = [_event("201220/22"), _event("934360/26", matricula="LV-XYZ"),
           _event("99999999/26", matricula="LV-NONE")]
_MANIFEST = {
    "00201220": [{"tipo": "IB", "path": "AE/IB-201220.pdf"},
                 {"tipo": "ISO", "path": "AE/ISO-201220.pdf"}],
    "00934360": [{"tipo": "IP", "path": "AE/IP-934360.pdf"}],
}
_ISO_URL = "https://so.jst.gob.ar/static/informes/AE/ISO-201220.pdf"
_IP_URL = "https://so.jst.gob.ar/static/informes/AE/IP-934360.pdf"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(jst, "DELAY", 0)


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_keeps_doc_bearing_only(conn):
    n = pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    assert n == 2  # the doc-less 99999999 stub skipped
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM jst_reports"))
    assert ids == ["00201220", "00934360"]


def test_discover_picks_iso_over_ib(conn):
    pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    row = conn.execute(
        "SELECT doc_tipo, doc_path, pdf_url, registration, occurrence_type "
        "FROM jst_reports WHERE case_id='00201220'").fetchone()
    assert row["doc_tipo"] == "ISO"
    assert row["doc_path"] == "AE/ISO-201220.pdf"
    assert row["pdf_url"] == _ISO_URL
    assert row["registration"] == "LV-ABC"
    assert row["occurrence_type"] == "Accidente"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST)) == 2
    assert pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST)) == 0


def test_discover_paginates_until_short_page(conn):
    # page 1 = full 20 doc-bearing, page 2 = 1 event then stop
    p1 = [_event(f"{i:08d}/20", matricula=f"LV-{i:04d}") for i in range(1, 21)]
    p2 = [_event("00000099/20", matricula="LV-LAST")]
    manifest = {f"{i:08d}": [{"tipo": "IB", "path": f"AE/{i}.pdf"}]
                for i in list(range(1, 21)) + [99]}
    client = FakeClient([p1, p2], manifest)
    n = pipeline.discover(conn, client)
    assert n == 21
    # page 2 was requested (page 1 was full → paginate), page 3 was not
    assert any("pagina=2" in u for u in client.requested)
    assert not any("pagina=3" in u for u in client.requested)


def test_discover_respects_max_pages(conn):
    p1 = [_event(f"{i:08d}/20", matricula=f"LV-{i:04d}") for i in range(1, 21)]
    p2 = [_event("00000099/20")]
    manifest = {f"{i:08d}": [{"tipo": "IB", "path": f"AE/{i}.pdf"}]
                for i in list(range(1, 21)) + [99]}
    client = FakeClient([p1, p2], manifest)
    pipeline.discover(conn, client, max_pages=1)
    assert not any("pagina=2" in u for u in client.requested)


# ── fetch ─────────────────────────────────────────────────────────────────────

def test_fetch_success_parses(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    pdfs = {_ISO_URL: b"%PDF iso", _IP_URL: b"%PDF ip"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 6000)
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs=pdfs),
                   pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT status, source_tier FROM jst_reports WHERE case_id='00201220'"
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "pdf"


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs={}),
                   pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM jst_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    pdfs = {_ISO_URL: b"%PDF", _IP_URL: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs=pdfs),
                   pdf_dir=str(tmp_path))
    tiers = {r["source_tier"] for r in conn.execute(
        "SELECT source_tier FROM jst_reports")}
    assert tiers == {"scanned"}


# ── build ─────────────────────────────────────────────────────────────────────

def _discover_fetch(conn, tmp_path, monkeypatch, text="N" * 6000):
    pipeline.discover(conn, FakeClient([_EVENTS], _MANIFEST))
    pdfs = {_ISO_URL: b"%PDF", _IP_URL: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs=pdfs),
                   pdf_dir=str(tmp_path))


def test_build(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch)
    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM jst_accidents")}
    assert acc["00201220"]["country"] == "AR"
    assert acc["00201220"]["event_date"] == "2020-01-01"
    assert acc["00201220"]["report_type"] == "ISO"
    assert acc["00201220"]["source_url"] == _ISO_URL
    assert acc["00934360"]["report_type"] == "IP"


def test_build_floor_skips_short(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch, text="tiny")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM jst_reports WHERE status='skipped'"
    ).fetchone()["c"] == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch)
    pipeline.build(conn)
    conn.execute("UPDATE jst_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM jst_accidents").fetchone()["c"] == 2
