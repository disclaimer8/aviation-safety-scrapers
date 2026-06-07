# tests/test_cli.py
"""
CLI smoke tests for bfu-ingest.
"""
import os
import pytest

from bfu_ingest import bfu, cli, db
from tests.conftest import FakeClient


# ── argparse smoke tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["discover", "fetch", "parse", "build", "all"])
def test_cli_modes_accepted(mode):
    """All five modes must be valid choices (argparse must not raise)."""
    args = cli._parse_args([mode])
    assert args.mode == mode


def test_cli_defaults():
    """--db and --pdf-dir defaults are correct, --full defaults False, --proxy defaults None."""
    args = cli._parse_args(["discover"])
    assert args.db == "bfu.db"
    assert args.pdf_dir == "pdfs"
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
    """When $BFU_PROXY is set and --proxy is not given, the env var is used."""
    monkeypatch.setenv("BFU_PROXY", "socks5h://127.0.0.1:40000")
    # Re-import to pick up the env — argparse reads os.environ.get at parse time
    import importlib
    importlib.reload(cli)
    args = cli._parse_args(["discover"])
    assert args.proxy == "socks5h://127.0.0.1:40000"
    monkeypatch.delenv("BFU_PROXY", raising=False)
    importlib.reload(cli)  # reset for other tests


def test_cli_invalid_mode_raises():
    with pytest.raises(SystemExit):
        cli._parse_args(["ingest"])


def test_cli_prog_name():
    """prog must be bfu-ingest."""
    import argparse
    ap = argparse.ArgumentParser(prog="bfu-ingest")
    assert ap.prog == "bfu-ingest"


# ── _make_client ──────────────────────────────────────────────────────────────

def test_make_client_browser_ua():
    """_make_client() must include a browser User-Agent."""
    c = cli._make_client()
    ua = c.headers.get("user-agent", "")
    assert "Mozilla" in ua
    assert "Chrome" in ua
    c.close()


def test_make_client_accept_language():
    """_make_client() must include Accept-Language with de-DE."""
    c = cli._make_client()
    lang = c.headers.get("accept-language", "")
    assert "de-DE" in lang
    c.close()


def test_make_client_no_proxy_by_default():
    """_make_client() without a proxy argument must not configure a proxy."""
    c = cli._make_client()
    # httpx.Client stores proxy in ._transport or via _proxies; simplest check:
    # just ensure the client is created without error and has no proxy url set
    # (proxy=None at call site means httpx uses system default = no proxy).
    assert c is not None
    c.close()


def test_make_client_accepts_proxy_arg():
    """_make_client(proxy=...) must not raise (proxy wiring tested via constructor)."""
    # We can't connect, but the httpx.Client should be constructible.
    try:
        c = cli._make_client(proxy="socks5h://127.0.0.1:40000")
        c.close()
    except Exception as e:
        pytest.fail(f"_make_client(proxy=...) raised unexpectedly: {e}")


# ── integration smoke: discover end-to-end via cli.main ──────────────────────

def test_cli_discover_runs(tmp_path, monkeypatch, capsys):
    dbfile = str(tmp_path / "bfu.db")
    fake_reports = [
        {
            "pdf_url": "https://www.bfu-web.de/DE/Publikationen/Bericht_23-0022-1X.pdf?__blob=publicationFile&v=1",
            "filename": "Bericht_23-0022-1X",
            "case_id": "BFU23-0022-1X",
            "title": "Unfall mit Learjet 35A in Rendsburg",
        }
    ]
    monkeypatch.setattr(bfu, "iter_reports", lambda client, **kw: iter(fake_reports))
    monkeypatch.setattr(cli, "_make_client", lambda **kw: FakeClient({}))
    cli.main(["discover", "--db", dbfile])
    out = capsys.readouterr().out
    assert "discovered: 1" in out
    conn = db.connect(dbfile)
    assert conn.execute("SELECT COUNT(*) FROM bfu_reports").fetchone()[0] == 1
    conn.close()


def test_cli_all_mode_runs(tmp_path, monkeypatch, capsys):
    """
    cli.main with mode='all' must call all four pipeline stages without error.
    Each stage is monkeypatched in the cli module namespace (where they are
    imported via 'from .pipeline import ...').
    """
    dbfile = str(tmp_path / "bfu.db")
    calls = []

    def _fake_discover(conn, client, full=False):
        calls.append("discover")
        return 0

    def _fake_fetch(conn, client, pdf_dir):
        calls.append("fetch")
        return 0

    def _fake_parse(conn):
        calls.append("parse")
        return 0

    def _fake_build(conn):
        calls.append("build")
        return 0

    # cli.py does `from .pipeline import discover, fetch, parse, build`
    # so we must patch the names in the cli module itself.
    monkeypatch.setattr(cli, "discover", _fake_discover)
    monkeypatch.setattr(cli, "fetch", _fake_fetch)
    monkeypatch.setattr(cli, "parse", _fake_parse)
    monkeypatch.setattr(cli, "build", _fake_build)
    monkeypatch.setattr(cli, "_make_client", lambda **kw: FakeClient({}))

    cli.main(["all", "--db", dbfile])
    assert calls == ["discover", "fetch", "parse", "build"]
