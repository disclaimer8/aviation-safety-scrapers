import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "listing_page.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_empty_html():
    return (FIXTURES / "listing_empty.html").read_text(encoding="utf-8")


@pytest.fixture
def detail_recent_html():
    return (FIXTURES / "detail_recent.html").read_text(encoding="utf-8")


@pytest.fixture
def detail_old_html():
    return (FIXTURES / "detail_old.html").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from uzpln_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
