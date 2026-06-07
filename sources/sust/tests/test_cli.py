from sust_ingest.cli import _parse_args


def test_modes():
    for mode in ("discover", "fetch", "build", "all"):
        assert _parse_args([mode]).mode == mode


def test_defaults():
    a = _parse_args(["all"])
    assert a.db == "sust.db"
    assert a.pdf_dir == "pdfs"
    assert a.max_rows is None


def test_overrides():
    a = _parse_args(["fetch", "--db", "x.db", "--pdf-dir", "/tmp/p",
                     "--max-rows", "3", "--proxy", "socks5h://h:1"])
    assert a.db == "x.db"
    assert a.pdf_dir == "/tmp/p"
    assert a.max_rows == 3
    assert a.proxy == "socks5h://h:1"
