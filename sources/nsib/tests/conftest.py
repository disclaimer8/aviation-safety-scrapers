import os
import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def air_reports_html():
    with open(os.path.join(FIXTURES, "nsib_air_reports.html"), encoding="utf-8") as fh:
        return fh.read()
