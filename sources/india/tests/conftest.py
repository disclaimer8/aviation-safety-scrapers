import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def index_html():
    return (FIXTURES / "index.html").read_text()


@pytest.fixture
def title_new():
    return (FIXTURES / "title-new.txt").read_text()


@pytest.fixture
def title_old():
    return (FIXTURES / "title-old.txt").read_text()


@pytest.fixture
def conn(tmp_path):
    from india_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
