import pytest


class FakeResp:
    def __init__(self, *, content=b"", status_code=200, text=None):
        self.content = content
        self.status_code = status_code
        self._text = text

    @property
    def text(self):
        if self._text is not None:
            return self._text
        return self.content.decode("utf-8", "replace") if isinstance(self.content, bytes) else self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Minimal httpx.Client stand-in. `routes` maps a URL (ignoring query) to a
    FakeResp or callable(url) -> FakeResp."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append(url)
        base = url.split("?")[0]
        handler = self.routes.get(url) or self.routes.get(base)
        if handler is None:
            return FakeResp(status_code=404)
        return handler(url) if callable(handler) else handler

    def close(self):
        pass


@pytest.fixture
def make_client():
    return lambda routes: FakeClient(routes)
