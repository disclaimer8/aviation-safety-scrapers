"""Ghana's discover swallowed every listing failure.

`except Exception: continue` over the three listing URLs meant a bureau that
was unreachable, or serving 502s, produced "discovered: 0" and exit 0. The
module is importable (it guards its entry point with __name__ == "__main__"),
so this drives the real code rather than parsing it.
"""
import sqlite3

import pytest

import ghana_scraper as g


class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.content = b""
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Serves one listing with a report PDF; raises or 502s for the others."""

    def __init__(self, dead=(), status=None):
        self.dead = set(dead)
        self.status = status or {}
        self.seen = []

    def get(self, url, **kw):
        self.seen.append(url)
        if url in self.dead:
            raise RuntimeError("connection reset")
        if url in self.status:
            return _Resp(status_code=self.status[url])
        return _Resp(text='<a href="/wp-content/uploads/final-report-9G-AAA.pdf">r</a>')


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the script at a temp DB and stub out its HTTP client."""
    monkeypatch.setattr(g, "DB", str(tmp_path / "ghana.db"))
    monkeypatch.setattr(g, "PDFDIR", str(tmp_path / "pdfs"))
    monkeypatch.setattr(g, "DELAY", 0)
    monkeypatch.setattr(g.sys, "argv", ["ghana_scraper.py", "discover"])
    return tmp_path


def _run_discover(monkeypatch, client):
    monkeypatch.setattr(g.httpx, "Client", lambda **kw: client)
    g.main()


class TestDiscoverMustNotSwallowListingFailures:
    def test_an_unreachable_listing_exits_non_zero(self, wired, monkeypatch):
        client = _Client(dead=g.LISTS)
        with pytest.raises(SystemExit) as e:
            _run_discover(monkeypatch, client)
        assert e.value.code == 1

    def test_a_502_listing_exits_non_zero(self, wired, monkeypatch):
        client = _Client(status={u: 502 for u in g.LISTS})
        with pytest.raises(SystemExit) as e:
            _run_discover(monkeypatch, client)
        assert e.value.code == 1

    def test_rows_from_the_listings_that_worked_are_still_committed(self, wired, monkeypatch):
        # One listing dies, two serve: the run must fail, and keep the rows.
        client = _Client(dead={g.LISTS[0]})
        with pytest.raises(SystemExit):
            _run_discover(monkeypatch, client)
        c = sqlite3.connect(g.DB)
        assert c.execute("SELECT COUNT(*) FROM ghana_reports").fetchone()[0] == 1

    def test_a_healthy_run_still_exits_normally(self, wired, monkeypatch):
        _run_discover(monkeypatch, _Client())
        c = sqlite3.connect(g.DB)
        assert c.execute("SELECT COUNT(*) FROM ghana_reports").fetchone()[0] == 1


class TestTheClientIsNotBuiltWithTLSVerificationOff:
    def test_verify_is_not_false(self, wired, monkeypatch):
        captured = {}

        def _capture(**kw):
            captured.update(kw)
            return _Client()

        monkeypatch.setattr(g.httpx, "Client", _capture)
        g.main()
        assert captured.get("verify") is not False
