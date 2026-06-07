# tests/test_cli.py
"""
CLI smoke tests for bea-ingest.
"""
import pytest

from bea_ingest import bea, cli, db
from tests.conftest import FakeClient


# ── argparse smoke tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["discover", "fetch", "parse", "build", "all"])
def test_cli_modes_accepted(mode):
    """All five modes must be valid choices (argparse must not raise)."""
    import argparse
    ap = argparse.ArgumentParser(prog="bea-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="bea.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args([mode])
    assert args.mode == mode


def test_cli_defaults():
    """--db and --pdf-dir defaults are correct."""
    import argparse
    ap = argparse.ArgumentParser(prog="bea-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="bea.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(["discover"])
    assert args.db == "bea.db"
    assert args.pdf_dir == "pdfs"
    assert args.full is False


def test_cli_full_flag():
    import argparse
    ap = argparse.ArgumentParser(prog="bea-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    ap.add_argument("--db", default="bea.db")
    ap.add_argument("--pdf-dir", default="pdfs")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args(["discover", "--full"])
    assert args.full is True


def test_cli_invalid_mode_raises():
    import argparse
    ap = argparse.ArgumentParser(prog="bea-ingest")
    ap.add_argument("mode", choices=["discover", "fetch", "parse", "build", "all"])
    with pytest.raises(SystemExit):
        ap.parse_args(["ingest"])


# ── integration smoke: discover end-to-end via cli.main ──────────────────────

def test_cli_discover_runs(tmp_path, monkeypatch, capsys):
    dbfile = str(tmp_path / "bea.db")
    fake_events = [
        {
            "slug": "cessna-208-f-hfdz-2026-05-24",
            "detail_url": "https://bea.aero/en/investigation-reports/notified-events/detail/cessna-208-f-hfdz-2026-05-24/",
            "title": "Accident to the Cessna 208 registered F-HFDZ on 24/05/2026 at Frétoy-le-Château AD",
        }
    ]
    monkeypatch.setattr(bea, "iter_events", lambda client: iter(fake_events))
    monkeypatch.setattr(cli, "_make_client", lambda: FakeClient({}))
    cli.main(["discover", "--db", dbfile])
    out = capsys.readouterr().out
    assert "discovered: 1" in out
    conn = db.connect(dbfile)
    assert conn.execute("SELECT COUNT(*) FROM bea_reports").fetchone()[0] == 1
    conn.close()


def test_cli_prog_name():
    """prog must be bea-ingest (not aaib-ingest)."""
    import argparse
    ap = argparse.ArgumentParser(prog="bea-ingest")
    assert ap.prog == "bea-ingest"
