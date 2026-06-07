import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing1_html():
    return (FIXTURES / "listing_page1.html").read_text(encoding="utf-8")


@pytest.fixture
def listing2_html():
    return (FIXTURES / "listing_page2.html").read_text(encoding="utf-8")


@pytest.fixture
def dtl_262906_html():
    return (FIXTURES / "dtl_262906.html").read_text(encoding="utf-8")


@pytest.fixture
def dtl_247386_html():
    return (FIXTURES / "dtl_247386.html").read_text(encoding="utf-8")


@pytest.fixture
def stub_html():
    return (FIXTURES / "redirect_stub.html").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from araib_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
