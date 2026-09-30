"""onderzoeksraad.nl's bot check (2026-09).

From some date in August the site answers a browser-looking client with a
200 "One moment... Checking your browser" page (header x-hv-flag: challenged)
whose script fetches /__hv_token, which sets an hv_pass cookie, then reloads.
discover() read that page as a listing with zero links and failed every week.
"""
import pytest

from ovv_ingest import ovv

_CHALLENGE = (
    "<!DOCTYPE html><html><head><title>One moment...</title></head><body>"
    "<p>Checking your browser, please wait...</p>"
    "<script>fetch('/__hv_token', {credentials: 'same-origin'})"
    ".then(function(){ location.reload(); });</script></body></html>"
)
_D1 = "https://onderzoeksraad.nl/en/onderzoek/crash-ph-abc-somewhere/"
_LISTING = f'<a href="{_D1}">x</a>'


class _Resp:
    def __init__(self, text="", content=b"", headers=None):
        self.text = text
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        pass


class _BotCheckClient:
    """Serves the challenge until /__hv_token has been fetched once."""

    def __init__(self, pass_token=True):
        self.passed = False
        self.pass_token = pass_token
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == ovv.BASE + "/__hv_token":
            self.passed = self.pass_token
            return _Resp(headers={"set-cookie": "hv_pass=1"})
        if not self.passed:
            return _Resp(text=_CHALLENGE, headers={"x-hv-flag": "challenged"})
        if url == ovv.LISTING_URL:
            return _Resp(text=_LISTING)
        return _Resp(content=b"%PDF main")


def test_listing_passes_the_bot_check_and_returns_links():
    client = _BotCheckClient()
    html = ovv.fetch_listing_page(client, 1)
    assert [r["slug"] for r in ovv.parse_listing(html)] == ["crash-ph-abc-somewhere"]
    assert client.requested.count(ovv.BASE + "/__hv_token") == 1


def test_a_check_that_will_not_clear_raises_instead_of_reading_as_empty():
    with pytest.raises(RuntimeError, match="bot check"):
        ovv.fetch_listing_page(_BotCheckClient(pass_token=False), 1)


def test_pdf_downloads_pass_the_check_too(tmp_path):
    dest = ovv.download_pdf(_BotCheckClient(), "https://onderzoeksraad.nl/x-pdf/", tmp_path / "x.pdf")
    assert open(dest, "rb").read() == b"%PDF main"
