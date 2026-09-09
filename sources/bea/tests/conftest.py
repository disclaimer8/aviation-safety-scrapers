# tests/conftest.py
import json as _json
import pytest


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

from bea_ingest import bea

# ── test speed ───────────────────────────────────────────────────────────────
# The politeness DELAY is a production setting. Left live here it buys nothing
# but wall clock, and CI runs every package under a 300s timeout.
@pytest.fixture(autouse=True)
def _no_politeness_delay_in_tests(monkeypatch):
    monkeypatch.setattr(bea, "DELAY", 0)
