# tests/conftest.py
import json as _json
import pytest


class FakeResp:
    def __init__(self, *, json_data=None, content=b"", text="", status_code=200):
        self._json = json_data
        self.content = content
        self._text = text
        self.status_code = status_code

    @property
    def text(self):
        return self._text or self.content.decode("utf-8", "replace")

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

    def get(self, url, params=None, **kwargs):
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
