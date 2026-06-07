import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def events_page():
    """One live-captured aviation events-API page (20 events)."""
    return json.loads((FIXTURES / "events_page1.json").read_text())["expedientes"]


@pytest.fixture
def manifest_slice():
    """Trimmed live Index.json slice (ISO+IB, IP-only, etc.)."""
    return json.loads((FIXTURES / "index_slice.json").read_text())


@pytest.fixture
def conn(tmp_path):
    from jst_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
