import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def hub_html():
    return _fx("hub.html")


@pytest.fixture
def cat_year_html():
    return _fx("cat_year.html")


@pytest.fixture
def cat_flat_html():
    return _fx("cat_flat.html")


@pytest.fixture
def year2024_html():
    return _fx("year_2024.html")


@pytest.fixture
def report_recent_html():
    return _fx("report_recent.html")


@pytest.fixture
def report_old_html():
    return _fx("report_old.html")


@pytest.fixture
def report_zwischen_html():
    return _fx("report_zwischen.html")


@pytest.fixture
def conn(tmp_path):
    from sub_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
