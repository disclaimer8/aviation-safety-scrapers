# tests/test_cli.py
from gpiaaf_ingest.cli import _parse_args


def test_modes():
    for mode in ("discover", "fetch", "build", "all"):
        assert _parse_args([mode]).mode == mode


def test_defaults():
    a = _parse_args(["all"])
    assert a.db == "gpiaaf.db"
    assert a.pdf_dir == "pdfs"
    assert a.headed is False
    assert a.max_years is None
    assert a.max_rows is None
    assert a.max_pdfs is None


def test_overrides():
    a = _parse_args(["discover", "--db", "x.db", "--pdf-dir", "/tmp/p",
                     "--headed", "--max-years", "2", "--max-rows", "5",
                     "--max-pdfs", "3"])
    assert a.db == "x.db"
    assert a.pdf_dir == "/tmp/p"
    assert a.headed is True
    assert a.max_years == 2
    assert a.max_rows == 5
    assert a.max_pdfs == 3
