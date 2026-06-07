import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_rows():
    return json.loads((FIXTURES / "listing.json").read_text())["Message"]


@pytest.fixture
def conn(tmp_path):
    from knkt_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
