import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def main_html():
    return (FIXTURES / "main.html").read_text()


@pytest.fixture
def archive_html():
    return (FIXTURES / "archive.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from sacaa_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
