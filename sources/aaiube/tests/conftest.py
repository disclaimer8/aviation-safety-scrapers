import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "listing.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from aaiube_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
