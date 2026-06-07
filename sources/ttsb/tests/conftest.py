import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def en_list_html():
    return (FIXTURES / "en_list.html").read_text(encoding="utf-8")


@pytest.fixture
def zh_list_html():
    return (FIXTURES / "zh_list.html").read_text(encoding="utf-8")


@pytest.fixture
def en_detail_html():
    return (FIXTURES / "en_detail.html").read_text(encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    from ttsb_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
