from aaib_ingest import cli, db, govuk
from tests.conftest import FakeResp, FakeClient


def test_cli_discover_runs(tmp_path, monkeypatch, capsys):
    dbfile = str(tmp_path / "a.db")

    def page(url, params):
        if params["start"] != 0:
            return FakeResp(json_data={"total": 1, "results": []})
        return FakeResp(json_data={"total": 1, "results": [
            {"link": "/aaib-reports/z", "title": "Z", "public_timestamp": "2026-01-01"}]})

    monkeypatch.setattr(cli, "_make_client", lambda: FakeClient({govuk.SEARCH_URL: page}))
    cli.main(["discover", "--db", dbfile])
    out = capsys.readouterr().out
    assert "discovered: 1" in out
    conn = db.connect(dbfile)
    assert conn.execute("SELECT COUNT(*) FROM aaib_reports").fetchone()[0] == 1
