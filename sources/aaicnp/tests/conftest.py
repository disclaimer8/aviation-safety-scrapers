# tests/conftest.py
import pytest


class FakeResp:
    def __init__(self, *, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    @property
    def text(self):
        return self.content.decode("utf-8", "replace") if isinstance(self.content, bytes) else self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Minimal stand-in for httpx.Client. `routes` maps a URL (full, incl. any
    ?page=N) to a FakeResp or a callable(url)->FakeResp. Unknown URLs -> 404."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return FakeResp(status_code=404)
        return handler(url) if callable(handler) else handler

    def close(self):
        pass


@pytest.fixture
def make_client():
    return lambda routes: FakeClient(routes)
