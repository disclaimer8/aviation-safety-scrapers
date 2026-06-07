import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def yearpage_html():
    return (FIXTURES / "yearpage.html").read_text()


@pytest.fixture
def case_html():
    return (FIXTURES / "case.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from aibdk_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
