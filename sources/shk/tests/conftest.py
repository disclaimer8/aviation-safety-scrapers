import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def sitemap_xml():
    return (FIXTURES / "sitemap-slice.xml").read_text()


@pytest.fixture
def detail_completed():
    return (FIXTURES / "detail-completed.html").read_text()


@pytest.fixture
def detail_ongoing():
    return (FIXTURES / "detail-ongoing.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from shk_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
