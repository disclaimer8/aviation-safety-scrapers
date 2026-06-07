import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def skeleton_html():
    return (FIXTURES / "skeleton.html").read_text()


@pytest.fixture
def entry_multi():
    return json.loads((FIXTURES / "entry_multi.json").read_text())


@pytest.fixture
def entry_single():
    return json.loads((FIXTURES / "entry_single.json").read_text())


@pytest.fixture
def entry_docless():
    return json.loads((FIXTURES / "entry_docless.json").read_text())


@pytest.fixture
def conn(tmp_path):
    from sust_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
