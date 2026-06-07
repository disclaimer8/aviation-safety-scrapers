from taic_ingest.cli import _parse_args


def test_modes():
    for mode in ("discover", "fetch", "build", "all"):
        assert _parse_args([mode]).mode == mode


def test_defaults():
    a = _parse_args(["all"])
    assert a.db == "taic.db"
    assert a.pdf_dir == "pdfs"
    assert a.max_pages is None


def test_overrides():
    a = _parse_args(["discover", "--db", "x.db", "--max-pages", "2",
                     "--pdf-dir", "/tmp/p", "--proxy", "socks5h://h:1"])
    assert a.db == "x.db"
    assert a.max_pages == 2
    assert a.pdf_dir == "/tmp/p"
    assert a.proxy == "socks5h://h:1"
