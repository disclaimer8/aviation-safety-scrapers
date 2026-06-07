# tests/conftest.py
from pathlib import Path
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class FakeResp:
    def __init__(self, *, text="", content=b"", status_code=200):
        self.text = text
        self.content = content or text.encode("utf-8")
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Minimal stand-in for httpx.Client. `routes` maps URL -> FakeResp or
    callable(url, **kw) -> FakeResp."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, **kw):
        self.calls.append(url)
        handler = self.routes.get(url)
        if handler is None:
            return FakeResp(status_code=404)
        return handler(url) if callable(handler) else handler

    def close(self):
        pass


@pytest.fixture
def read_fixture():
    return _read


@pytest.fixture
def make_client():
    return lambda routes: FakeClient(routes)
