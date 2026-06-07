import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def rest_rows():
    return json.loads((FIXTURES / "reports.json").read_text())


@pytest.fixture
def conn(tmp_path):
    from aaiu_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
