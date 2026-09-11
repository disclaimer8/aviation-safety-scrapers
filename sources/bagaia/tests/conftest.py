# tests/conftest.py
import sqlite3
import pytest
from bagaia_ingest import db as _db


@pytest.fixture
def conn(tmp_path):
    c = _db.connect(str(tmp_path / "test.db"))
    _db.init_schema(c)
    yield c
    c.close()
