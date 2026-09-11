import pathlib
import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def index_html():
    return (FIXTURES / "aaibzm_index.html").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def sample_report_text():
    return (FIXTURES / "sample_report_text.txt").read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def cover_block_text():
    return (FIXTURES / "sample_cover_block.txt").read_text(encoding="utf-8", errors="replace")
