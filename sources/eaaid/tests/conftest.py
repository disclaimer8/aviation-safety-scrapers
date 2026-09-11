# tests/conftest.py
import pytest

from eaaid_ingest import pipeline


class FakeResp:
    def __init__(self, *, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Stand-in for the httpc client. `routes` maps URL -> FakeResp or callable."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        h = self.routes.get(url)
        if h is None:
            return FakeResp(status_code=404)
        return h(url) if callable(h) else h

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_politeness_delay(monkeypatch):
    """fetch() sleeps FETCH_DELAY per document; the suite must not."""
    monkeypatch.setattr(pipeline, "FETCH_DELAY", 0.0)
