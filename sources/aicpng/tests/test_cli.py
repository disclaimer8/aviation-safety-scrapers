"""CLI smoke: argument parsing + dispatch wiring."""
from aicpng_ingest import cli


def test_cli_make_client_no_proxy():
    c = cli._make_client()
    assert c is not None
    c.close()


def test_cli_discover_runs(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(cli, "discover", lambda conn, client, full=False: calls.setdefault("d", 5) or 5)
    monkeypatch.setattr(cli, "_make_client", lambda proxy=None: type("C", (), {"close": lambda s: None})())
    cli.main(["discover", "--db", str(tmp_path / "t.db")])
    assert calls.get("d") == 5
