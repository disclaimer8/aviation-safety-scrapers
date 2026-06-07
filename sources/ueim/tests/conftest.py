import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "hava_araci.html").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from ueim_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
