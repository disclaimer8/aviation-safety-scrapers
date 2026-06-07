import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "listing-p0.html").read_text()


@pytest.fixture
def detail_html():
    return (FIXTURES / "detail.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from nsia_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
