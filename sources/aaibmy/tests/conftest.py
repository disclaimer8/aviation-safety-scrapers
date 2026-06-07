import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def hub_html():
    return (FIXTURES / "hub.html").read_text(encoding="utf-8")


@pytest.fixture
def year2022_html():
    return (FIXTURES / "year_2022.html").read_text(encoding="utf-8")


@pytest.fixture
def year2024_html():
    return (FIXTURES / "year_2024.html").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from aaibmy_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
