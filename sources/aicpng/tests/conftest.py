# tests/conftest.py
import json as _json
import pytest

from aicpng_ingest import aicpng


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    """Zero the inter-request delay for every test in this package.

    pipeline.fetch() sleeps aicpng.DELAY (2.0s) per document, and two tests
    walk all 20 fixtures, so the suite spent 255s of its 255s runtime asleep.
    CI allows `timeout 300` per package — this was passing with 45s of margin
    on a quiet runner and would have started flaking on a busy one.

    autouse and declared here rather than in one test module: a fixture in
    test_pipeline.py does not apply to the others, which is how the same
    problem survived in pkbwl until it was found a second time.
    """
    monkeypatch.setattr(aicpng, "DELAY", 0.0)


class FakeResp:
    def __init__(self, *, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Minimal stand-in for httpx.Client. `routes` maps a URL (ignoring query
    params) to a FakeResp or a callable(url, params) -> FakeResp."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        handler = self.routes.get(url)
        if handler is None:
            return FakeResp(status_code=404)
        return handler(url, params) if callable(handler) else handler

    def close(self):
        pass


@pytest.fixture
def make_client():
    return lambda routes: FakeClient(routes)
