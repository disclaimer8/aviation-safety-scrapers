# tests/conftest.py
import pytest


class FakeResp:
    def __init__(self, *, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Minimal stand-in for httpx.Client."""

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
