import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def items_payload():
    """Live-captured GCAA SharePoint response (OData verbose, 8-item slice)."""
    return json.loads((FIXTURES / "items_response.json").read_text())


@pytest.fixture
def items(items_payload):
    """The 8 raw item dicts (unwrapped from d.results)."""
    return items_payload["d"]["results"]


@pytest.fixture
def conn(tmp_path):
    from gcaa_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
