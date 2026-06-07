import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "listing.html").read_text(encoding="utf-8")


@pytest.fixture
def new_pdf_head():
    return (FIXTURES / "new_pdf_head.txt").read_text(encoding="utf-8")


@pytest.fixture
def old_pdf_head():
    return (FIXTURES / "old_pdf_head.txt").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from tsib_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
