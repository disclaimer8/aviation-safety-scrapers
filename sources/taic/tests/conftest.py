import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def listing_html():
    return (FIXTURES / "listing-page0.html").read_text()


@pytest.fixture
def listing_empty_html():
    return (FIXTURES / "listing-empty.html").read_text()


@pytest.fixture
def inquiry_rich_html():
    return (FIXTURES / "inquiry-rich.html").read_text()


@pytest.fixture
def inquiry_old_html():
    return (FIXTURES / "inquiry-old-thin.html").read_text()


@pytest.fixture
def inquiry_in_progress_html():
    return (FIXTURES / "inquiry-in-progress.html").read_text()


@pytest.fixture
def conn(tmp_path):
    from taic_ingest import db

    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()
