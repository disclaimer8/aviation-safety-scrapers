"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from jst_ingest import db, jst, pipeline


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
        if "intranet" in url:   # the old route; nothing should ask for it now
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
_ISO_PATH = "AE/2022/021922-00201220/ISO-00201220-22.pdf"
_IP_PATH = "AE/2026/010426-00934360/IP-00934360-26.pdf"
_ISO_URL = "https://so.jst.gob.ar/static/informes/" + _ISO_PATH
_IP_URL = "https://so.jst.gob.ar/static/informes/" + _IP_PATH


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(jst, "DELAY", 0)


# ── discover ──────────────────────────────────────────────────────────────────
#
# The discover tests that lived here drove the intranet events walk, which is
# gone: that host answers `Disallow: /`. The manifest route that replaced it is
# covered end to end in test_allowed_route.py, against fixtures taken from real
# reports. What remains below is fetch and build, seeded directly.


def _seed_new(conn, case_id, doc_tipo="ISO", doc_path=None, date="2022-02-19"):
    """One discovered row, as the manifest route produces it: identifiers and
    the path date, with the report's own fields still empty."""
    doc_path = doc_path or f"AE/2022/021922-{case_id}/{doc_tipo}-{case_id}-22.pdf"
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO jst_reports (case_id, doc_path, doc_tipo, "
        "date_of_occurrence, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (case_id, doc_path, doc_tipo, date, jst.pdf_url(doc_path),
         db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_fetch_success_parses(conn, tmp_path, monkeypatch):
    _seed_new(conn, "00201220", doc_path=_ISO_PATH)
    _seed_new(conn, "00934360", doc_tipo="IP", doc_path=_IP_PATH,
              date="2026-01-04")
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
    _seed_new(conn, "00201220", doc_path=_ISO_PATH)
    _seed_new(conn, "00934360", doc_tipo="IP", doc_path=_IP_PATH, date="2026-01-04")
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs={}),
                   pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM jst_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    _seed_new(conn, "00201220", doc_path=_ISO_PATH)
    _seed_new(conn, "00934360", doc_tipo="IP", doc_path=_IP_PATH,
              date="2026-01-04")
    pdfs = {_ISO_URL: b"%PDF", _IP_URL: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs=pdfs),
                   pdf_dir=str(tmp_path))
    tiers = {r["source_tier"] for r in conn.execute(
        "SELECT source_tier FROM jst_reports")}
    assert tiers == {"scanned"}


# ── build ─────────────────────────────────────────────────────────────────────

def _discover_fetch(conn, tmp_path, monkeypatch, text="N" * 6000):
    _seed_new(conn, "00201220", doc_path=_ISO_PATH)
    _seed_new(conn, "00934360", doc_tipo="IP", doc_path=_IP_PATH,
              date="2026-01-04")
    pdfs = {_ISO_URL: b"%PDF", _IP_URL: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    pipeline.fetch(conn, FakeClient([_EVENTS], _MANIFEST, pdfs=pdfs),
                   pdf_dir=str(tmp_path))


def test_build(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch)
    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM jst_accidents")}
    assert acc["00201220"]["country"] == "AR"
    # The date comes off the manifest path now (021922 = 19/02/22).
    assert acc["00201220"]["event_date"] == "2022-02-19"
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
