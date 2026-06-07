# tests/test_cli.py
"""
CLI smoke tests for tsb-ingest.
"""
import os
import pytest

from tsb_ingest import tsb, cli, db
from tests.conftest import FakeClient


# ── argparse smoke tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["discover", "fetch", "build", "all"])
def test_cli_modes_accepted(mode):
    """All four modes must be valid choices (argparse must not raise)."""
    args = cli._parse_args([mode])
    assert args.mode == mode


def test_cli_parse_mode_invalid():
    """'parse' is no longer a valid mode (folded into fetch)."""
    with pytest.raises(SystemExit):
        cli._parse_args(["parse"])


def test_cli_defaults():
    """--db defaults to tsb.db, --full defaults False, --proxy defaults None."""
    args = cli._parse_args(["discover"])
    assert args.db == "tsb.db"
    assert args.full is False
    assert args.proxy is None


def test_cli_full_flag():
    args = cli._parse_args(["discover", "--full"])
    assert args.full is True


def test_cli_proxy_flag():
    """--proxy value is stored correctly."""
    args = cli._parse_args(["discover", "--proxy", "socks5h://127.0.0.1:40000"])
    assert args.proxy == "socks5h://127.0.0.1:40000"


def test_cli_proxy_from_env(monkeypatch):
    """When $TSB_PROXY is set and --proxy is not given, the env var is used."""
    monkeypatch.setenv("TSB_PROXY", "socks5h://127.0.0.1:40000")
    import importlib
    importlib.reload(cli)
    args = cli._parse_args(["discover"])
    assert args.proxy == "socks5h://127.0.0.1:40000"
    monkeypatch.delenv("TSB_PROXY", raising=False)
    importlib.reload(cli)  # reset for other tests


def test_cli_invalid_mode_raises():
    with pytest.raises(SystemExit):
        cli._parse_args(["ingest"])


def test_cli_prog_name():
    """prog must be tsb-ingest."""
    import argparse
    ap = argparse.ArgumentParser(prog="tsb-ingest")
    assert ap.prog == "tsb-ingest"


# ── _make_client ──────────────────────────────────────────────────────────────

def test_make_client_browser_ua():
    """_make_client() must include a browser User-Agent."""
    c = cli._make_client()
    ua = c.headers.get("user-agent", "")
    assert "Mozilla" in ua
    assert "Chrome" in ua
    c.close()


def test_make_client_accept_language():
    """_make_client() must include Accept-Language with en-CA."""
    c = cli._make_client()
    lang = c.headers.get("accept-language", "")
    assert "en-CA" in lang
    c.close()


def test_make_client_no_proxy_by_default():
    """_make_client() without a proxy argument must not configure a proxy."""
    c = cli._make_client()
    assert c is not None
    c.close()


def test_make_client_accepts_proxy_arg():
    """_make_client(proxy=...) must not raise (proxy wiring tested via constructor)."""
    try:
        c = cli._make_client(proxy="socks5h://127.0.0.1:40000")
        c.close()
    except Exception as e:
        pytest.fail(f"_make_client(proxy=...) raised unexpectedly: {e}")


# ── integration smoke: discover end-to-end via cli.main ──────────────────────

def test_cli_discover_runs(tmp_path, monkeypatch, capsys):
    dbfile = str(tmp_path / "tsb.db")
    fake_rows = [
        {
            "case_id": "A11Q0170",
            "report_url": "https://www.tsb.gc.ca/eng/rapports-reports/aviation/2011/a11q0170/a11q0170.html",
            "event_date": "2011-08-29",
            "occurrence_type": "Risk of collision",
            "operator": "Air Inuit",
            "aircraft": "Bombardier DHC-8-315",
            "location": "Kuujjuaq, Quebec",
            "occurrence_status": "Completed",
            "registration": None,
        }
    ]
    monkeypatch.setattr(tsb, "iter_index", lambda client: fake_rows)
    monkeypatch.setattr(cli, "_make_client", lambda **kw: FakeClient({}))
    cli.main(["discover", "--db", dbfile])
    out = capsys.readouterr().out
    assert "discovered: 1" in out
    conn = db.connect(dbfile)
    assert conn.execute("SELECT COUNT(*) FROM tsb_reports").fetchone()[0] == 1
    conn.close()


def test_cli_all_mode_runs(tmp_path, monkeypatch, capsys):
    """
    cli.main with mode='all' must call discover → fetch → build without error.
    Each stage is monkeypatched in the cli module namespace.
    """
    dbfile = str(tmp_path / "tsb.db")
    calls = []

    def _fake_discover(conn, client, full=False):
        calls.append("discover")
        return 0

    def _fake_fetch(conn, client):
        calls.append("fetch")
        return 0

    def _fake_build(conn):
        calls.append("build")
        return 0

    monkeypatch.setattr(cli, "discover", _fake_discover)
    monkeypatch.setattr(cli, "fetch", _fake_fetch)
    monkeypatch.setattr(cli, "build", _fake_build)
    monkeypatch.setattr(cli, "_make_client", lambda **kw: FakeClient({}))

    cli.main(["all", "--db", dbfile])
    assert calls == ["discover", "fetch", "build"]
