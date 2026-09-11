# tests/conftest.py
import pytest

from aaicth_ingest import aaicth


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    """Zero the inter-request delay for every test in this package.

    pipeline sleeps aaicth.DELAY (2.0s) between requests and the suite walked
    enough fixtures to spend 48 of its 48 seconds asleep. CI allows
    `timeout 300` per package, so it passed — but that margin is what
    disappears on a busy runner.

    autouse and package-wide: a fixture in one test module does not reach the
    others, which is how the same problem survived in pkbwl and then aicpng.
    """
    monkeypatch.setattr(aaicth, "DELAY", 0.0)
