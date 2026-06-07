import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def listing_html():
    return _read("listing_page1.html")


@pytest.fixture
def detail_2456_html():
    return _read("detail_2022-2456.html")


@pytest.fixture
def detail_1098_html():
    return _read("detail_2015-1098.html")


@pytest.fixture
def detail_nopdf_html():
    return _read("detail_2026-0040_nopdf.html")


@pytest.fixture
def conn(tmp_path):
    from pkbwl_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
