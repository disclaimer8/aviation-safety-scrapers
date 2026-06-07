# tests/test_cli.py
from otkes_ingest.cli import _parse_args


def test_modes():
    for mode in ("discover", "fetch", "build", "all"):
        assert _parse_args([mode]).mode == mode


def test_defaults():
    a = _parse_args(["all"])
    assert a.db == "otkes.db"
    assert a.pdf_dir == "pdfs"
    assert a.headed is False
    assert a.max_listings is None
    assert a.max_details is None


def test_overrides():
    a = _parse_args(["discover", "--db", "x.db", "--pdf-dir", "/tmp/p",
                     "--headed", "--max-listings", "2", "--max-details", "5"])
    assert a.db == "x.db"
    assert a.pdf_dir == "/tmp/p"
    assert a.headed is True
    assert a.max_listings == 2
    assert a.max_details == 5
